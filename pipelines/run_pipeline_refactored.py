import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd
import yaml
from omegaconf import DictConfig

from eks.singlecam_smoother import ensemble_kalman_smoother_singlecam
from eks.utils import format_data, populate_output_dataframe
from pseudo_labeler.evaluation import (
    collect_preds,
    compute_ens_mean_median,
    compute_ood_snippet_metrics,
    find_model_dirs,
    process_csv_for_sessions_and_frames,
    run_eks_on_snippets,
    run_inference_on_snippets,
    run_ood_pipeline
)
from pseudo_labeler.frame_selection import (
    export_frames,
    select_frame_idxs_hand,
    select_frame_idxs_random,
    pick_n_hand_labels,
    process_predictions,
    update_seed_labels
)
from pseudo_labeler.train import inference_with_metrics, train, train_and_infer
from pseudo_labeler.utils import format_data_walk, pipeline_eks, load_cfgs, find_video_names

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../eks')))

def pipeline(config_file: str):

    # ------
    # Setup
    # ------

    # Load cfg (pipeline yaml) and cfg_lp (lp yaml)
    cfg, cfg_lp = load_cfgs(config_file)  # cfg_lp is a DictConfig, cfg is not

    # Define + create directories
    data_dir = cfg_lp.data.data_dir
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    outputs_dir = os.path.join(parent_dir, (
        f'../outputs/{os.path.basename(data_dir)}/'
        f'hand={cfg["n_hand_labels"]}_pseudo={cfg["n_pseudo_labels"]}'
        ))
    networks_dir = os.path.join(outputs_dir, 'networks')
    pp_dir = os.path.join(outputs_dir, 'post-processors',
        f"{cfg['pseudo_labeler']}_rng={cfg['ensemble_seeds'][0]}-{cfg['ensemble_seeds'][-1]}")
    os.makedirs(outputs_dir, exist_ok=True)
    os.makedirs(networks_dir, exist_ok=True)
    os.makedirs(pp_dir, exist_ok=True)

    # Build list of video names from the video directory
    num_videos, video_names = find_video_names(data_dir, cfg["video_directories"])
    print(f'Found {num_videos} videos: {video_names}.')
    
    # -------------------------------------------------------------------------------------
    # Train k supervised models on n hand-labeled frames and compute labeled OOD metrics
    # -------------------------------------------------------------------------------------  

    # Pick n hand labels. Make two csvs: one with the labels, one with the leftovers
    subsample_path, unsampled_path = pick_n_hand_labels(cfg, cfg_lp, data_dir, outputs_dir)

    # ||| Main first-round training loop |||
    # loops over ensemble seeds training a model for each seed with n hand_labels
    print(f'Training {len(cfg["ensemble_seeds"])} baseline models.')
    for k in cfg["ensemble_seeds"]:
        # Make directory for rng{seed}
        results_dir = os.path.join(networks_dir, f'rng{k}')
        os.makedirs(results_dir, exist_ok=True)
        # Main function call
        train_and_infer(
            cfg=cfg.copy(),
            cfg_lp=cfg_lp.copy(),
            k=k,
            data_dir=data_dir,
            results_dir=results_dir,
            new_labels_csv = subsample_path # Set to None to use the original csv_file
        )

    # # # -------------------------------------------------------------------------------------
    # # # Post-process network outputs to generate potential pseudo labels (chosen in the next step)
    # # # -------------------------------------------------------------------------------------
    pseudo_labeler = cfg["pseudo_labeler"]

    # Collect input csv names from video names; skip existing ones
    input_csv_names = []
    for video_name in video_names:
        csv_name = video_name.replace(".mp4", ".csv")
        csv_path = os.path.join(pp_dir, csv_name)
        if os.path.exists(csv_path):
            print(f"Post-processed output for {os.path.basename(csv_path)} already exists. Skipping.")
        else:
            input_csv_names.append(csv_name)
    
    print(f'Post-processing the following videos using {pseudo_labeler}: {input_csv_names}')
    # ||| Main EKS function call ||| pipeline_eks will also handle ensemble_mean baseline
    if pseudo_labeler == "eks" or pseudo_labeler == "ensemble_mean":
        pipeline_eks(input_csv_names, networks_dir, cfg["data_type"], pseudo_labeler, cfg_lp, pp_dir)


    # # -------------------------------------------------------------------------------------
    # # run inference on OOD snippets (if specified) -- using network models
    # # -------------------------------------------------------------------------------------
    dataset_name = os.path.basename(data_dir)
    if cfg["ood_snippets"]:
        print(f'Starting OOD snippet analysis for {dataset_name}')
        run_ood_pipeline(
            cfg=cfg,
            cfg_lp=cfg_lp,
            data_dir=data_dir,
            networks_dir=networks_dir,
            pp_dir=pp_dir,
            pseudo_labeler=pseudo_labeler
        )

    # # -------------------------------------------------------------------------------------
    # # select frames to add to the dataset
    # # -------------------------------------------------------------------------------------
    selection_strategy = cfg["selection_strategy"]
    print(
        f'Selecting {cfg["n_pseudo_labels"]} pseudo-labels from {num_videos} {cfg["pseudo_labeler"]} '
        f'outputs using ({selection_strategy} strategy)'
    )

    selected_frame_idxs = []    
    labeled_data_dir = os.path.join(data_dir, "labeled-data") 

    # Create a new directory for combined hand labels and pseudo labels
    hand_label_and_pseudo_label_dir = os.path.join(parent_dir, (
        f"../outputs/{os.path.basename(data_dir)}/hand={cfg['n_hand_labels']}_"
        f"pseudo={cfg['n_pseudo_labels']}/pseudo_label_and_hand_label"
    ))
    os.makedirs(hand_label_and_pseudo_label_dir, exist_ok=True)

    hand_labels = pd.read_csv(subsample_path, header=[0,1,2], index_col=0)
    # Process each ensemble seed
    for k in cfg["ensemble_seeds"]:
        # Initialize seed_labels with hand labels for this seed
        seed_labels = hand_labels.copy()
        frame_idxs = []
        preds_csv_path = None
        results_dir = os.path.join(
            parent_dir, (
                f"../outputs/{os.path.basename(data_dir)}/hand={cfg['n_hand_labels']}_"
                f"pseudo={cfg['n_pseudo_labels']}/results_aeks_{cfg['selection_strategy']}/rng{k}"
            )
        )
        combined_csv_filename = f"CollectedData_hand={cfg['n_hand_labels']}_pseudo={cfg['n_pseudo_labels']}_k={k}.csv"
        combined_csv_path = os.path.join(hand_label_and_pseudo_label_dir, combined_csv_filename)
        #if os.path.exists(combined_csv_path):
        #    print(f'Selected frames already exist at {combined_csv_path}. Skipping frame selection for rng{k}.')

        #else:
        print(f'Using a {selection_strategy} pseudo-label selection strategy.')

        if selection_strategy == 'random':
            frames_per_video = int(cfg["n_pseudo_labels"] / num_videos)
            print(f"Frames per video: {frames_per_video}")
            for video_dir in cfg["video_directories"]:
                video_files = os.listdir(os.path.join(data_dir, video_dir))
                for video_file in video_files:
                    video_path = os.path.join(data_dir, video_dir, video_file)
                    frame_idxs = select_frame_idxs_random(
                        video_file=video_path,
                        n_frames_to_select=frames_per_video,
                        seed=k
                    )
                    base_name = os.path.splitext(os.path.basename(video_file))[0]
                    csv_filename = base_name + ".csv"
                    preds_csv_path = os.path.join(parent_dir, (
                            f"../outputs/{os.path.basename(data_dir)}/hand={cfg['n_hand_labels']}_"
                            f"pseudo={cfg['n_pseudo_labels']}/post-processors/"
                            f"{cfg['pseudo_labeler']}_rng={cfg['ensemble_seeds'][0]}-{cfg['ensemble_seeds'][-1]}"
                        ),
                        csv_filename
                    )
                    selected_frame_idxs.extend(frame_idxs)
                    frame_idxs = frame_idxs.astype(int)
                    print(f'Selected frame indices (displaying first 10 of {len(frame_idxs)}): {frame_idxs[0:10]}...')
                    
                    export_frames(
                        video_file=video_path,
                        save_dir=os.path.join(labeled_data_dir, os.path.splitext(os.path.basename(video_file))[0]),
                        frame_idxs=frame_idxs,
                        format="png",
                        n_digits=8,
                        context_frames=0,
                    )
                    
                    preds_df = pd.read_csv(preds_csv_path, header=[0,1,2], index_col=0)
                    subselected_preds = process_predictions(preds_df, frame_idxs, base_name, generate_index=True)

                    seed_labels = update_seed_labels(seed_labels, subselected_preds)

        elif selection_strategy == 'hand':
            frame_idxs = select_frame_idxs_hand(
                hand_labels_csv=unsampled_path,
                n_frames_to_select=cfg["n_pseudo_labels"],
                seed=k
            )
            preds_csv_path = unsampled_path
            frame_idxs = frame_idxs.astype(int)
            print(f'Selected frame indices (displaying first 10 of {len(frame_idxs)}): {frame_idxs[0:10]}...')
            
            preds_df = pd.read_csv(preds_csv_path, header=[0,1,2], index_col=0)
            base_name = os.path.splitext(os.path.basename(preds_csv_path))[0]
            subselected_preds = process_predictions(preds_df, frame_idxs, base_name, generate_index=False)
            
            seed_labels = update_seed_labels(seed_labels, subselected_preds)

        # Export the combined hand labels and pseudo labels for this seed
        seed_labels.to_csv(combined_csv_path)
        print(f"Saved combined hand labels and pseudo labels for seed {k} to {combined_csv_path}")

        # Check number of labels for this seed
        expected_total_labels = cfg['n_hand_labels'] + cfg["n_pseudo_labels"]
        if seed_labels.shape[0] != expected_total_labels:
            print(f"Warning: Number of labels for seed {k} ({seed_labels.shape[0]}) does not match expected count ({expected_total_labels})")
        else:
            print(f"Label count verified for seed {k}: {seed_labels.shape[0]} labels")

        print(f"All combined hand labels and pseudo labels saved in {hand_label_and_pseudo_label_dir}")

        # # -------------------------------------------------------------------------------------
        # # Train models on expanded dataset
        # # -------------------------------------------------------------------------------------

        # Define the results directory for this seed
        results_dir = os.path.join(
            parent_dir, (
                f"../outputs/{os.path.basename(data_dir)}/hand={cfg['n_hand_labels']}_"
                f"pseudo={cfg['n_pseudo_labels']}/results_aeks_{cfg['selection_strategy']}/rng{k}"
            )
        )
        os.makedirs(results_dir, exist_ok=True)

        csv_prefix = (
            f"hand={cfg['n_hand_labels']}_rng={k}_"
            f"pseudo={cfg['n_pseudo_labels']}_"
            f"{cfg['pseudo_labeler']}_{cfg['selection_strategy']}_"
            f"rng={cfg['ensemble_seeds'][0]}-{cfg['ensemble_seeds'][-1]}"
        )

        # Run train_and_infer with the combined hand labels and pseudo labels
        train_and_infer(
            cfg=cfg.copy(),
            cfg_lp=cfg_lp.copy(),
            k=k,
            data_dir=data_dir,
            results_dir=results_dir,
            csv_prefix=csv_prefix,
            new_labels_csv=combined_csv_path,  # Use the combined CSV file for this seed
            n_train_frames=expected_total_labels
        )

        print(f"Completed training and inference for seed {k} using combined hand labels and pseudo labels")

    print("Completed training and inference for all seeds using expanded datasets")

    # # # -------------------------------------------------------------------------------------
    # # # Run EKS on expanded dataset inferences
    # # # -------------------------------------------------------------------------------------
    pseudo_labeler = 'eks'
    input_dir = os.path.join(parent_dir, (
            f"../outputs/{os.path.basename(data_dir)}/hand={cfg['n_hand_labels']}_"
            f"pseudo={cfg['n_pseudo_labels']}/results_aeks_{cfg['selection_strategy']}/"
        )
    )
    results_dir = os.path.join(parent_dir, (
            f"../outputs/{os.path.basename(data_dir)}/hand={cfg['n_hand_labels']}_"
            f"pseudo={cfg['n_pseudo_labels']}/results_aeks_{cfg['selection_strategy']}/"
            f"{pseudo_labeler}_rng={cfg['ensemble_seeds'][0]}-{cfg['ensemble_seeds'][-1]}"
        )
    )

    if os.path.exists(results_dir):
        print(f"\n\n\n\n{pseudo_labeler} directory {results_dir} already exists. Skipping post-processing\n.\n.\n.\n")
    else:
        print(f"Post-Processing Network Outputs using method: {pseudo_labeler}\n.\n.\n.\n")
        os.makedirs(results_dir, exist_ok=True)
        data_type = cfg["data_type"]
        output_df = None

        # Collect input csv names from video directory
        input_csv_names = []
        for video_dir in cfg["video_directories"]:
            video_files = os.listdir(os.path.join(data_dir, video_dir))
            for video_file in video_files:
                csv_name = video_file.replace(".mp4", ".csv")
                if csv_name not in input_csv_names:
                    print(f'Appending: {csv_name} to post-processing input csv list')
                    input_csv_names.append(csv_name)

        if pseudo_labeler == "eks" or pseudo_labeler == "ensemble_mean":
            pipeline_eks(input_csv_names, input_dir, data_type, pseudo_labeler, cfg_lp, results_dir)


    # # -------------------------------------------------------------------------------------
    # # run inference on OOD snippets (if specified) -- using network models
    # # -------------------------------------------------------------------------------------
    n_hand_labels = cfg['n_hand_labels']
    n_pseudo_labels = cfg['n_pseudo_labels']
    seeds = cfg['ensemble_seeds']
    # where the aeks models are stored
    aeks_dir = (
        f"/teamspace/studios/this_studio/outputs/{dataset_name}/"
        f"hand={n_hand_labels}_pseudo={n_pseudo_labels}/"
        f"results_aeks_{cfg['selection_strategy']}"
    )
    # where to save aeks_eks outputs
    aeks_eks_save_dir = (
        f"/teamspace/studios/this_studio/outputs/{dataset_name}/"
        f"hand={n_hand_labels}_pseudo={n_pseudo_labels}/"
        f"results_aeks_{cfg['selection_strategy']}/"
        f"eks_rng={seeds[0]}-{seeds[-1]}"
    )
    model_dirs_list = find_model_dirs(aeks_dir, 'rng')
    print(f"Found {len(model_dirs_list)} network model directories")

    if cfg["ood_snippets"]:

        # remove eks_rng=0-3
        for directory in model_dirs_list:
            if 'eks' in os.path.basename(directory):
                print(f"aEKS model directory {directory} had 'eks' in it and was removed")
                model_dirs_list.remove(directory)
        
        # Step 1: Run inference on video snippets
        ground_truth_df = pd.read_csv(os.path.join(data_dir, ground_truth_csv), skiprows=2)
        run_inference_on_snippets(model_dirs_list, data_dir, snippets_dir, ground_truth_df)

        # Step 2: Run EKS
        df_eks, dfs_markers = run_eks_on_snippets(snippets_dir, model_dirs_list, aeks_eks_save_dir, ground_truth_df, keypoint_ensemble_list)

        # Step 3.1: Collect preds from individual models
        collect_preds(model_dirs_list, snippets_dir)

        # Step 3.2: Compute ens mean and median
        compute_ens_mean_median(model_dirs_list, aeks_eks_save_dir, 'ens-mean')
        compute_ens_mean_median(model_dirs_list, aeks_eks_save_dir, 'ens-median')

        # Step 4: Compute metrics
        compute_ood_snippet_metrics(config_dir, dataset_name, data_dir, ground_truth_csv, model_dirs_list, aeks_eks_save_dir)
            

if __name__ == "__main__":
    # config_file = "../configs/pipeline_example.yaml"
    # pipeline(config_file)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--config',
        required=True,
        help='absolute path to .yaml configuration file',
        type=str,
    )
    args = parser.parse_args()
    pipeline(args.config)

 