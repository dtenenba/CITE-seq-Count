#!/usr/bin/env python3.6
"""
Author: Patrick Roelli
"""
import sys
import os
import logging
import gzip
import requests
import time

from collections import OrderedDict, Counter, defaultdict, namedtuple

# pylint: disable=no-name-in-module
from multiprocess import Pool, Queue, JoinableQueue, Process

from cite_seq_count import preprocessing
from cite_seq_count import processing
from cite_seq_count import chemistry
from cite_seq_count import io
from cite_seq_count import secondsToText
from cite_seq_count import argsparser


def main():
    # Create logger and stream handler
    logger = logging.getLogger("cite_seq_count")
    logger.setLevel(logging.CRITICAL)
    ch = logging.StreamHandler()
    ch.setLevel(logging.CRITICAL)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    start_time = time.time()
    parser = argsparser.get_args()
    if not sys.argv[1:]:
        parser.print_help(file=sys.stderr)
        sys.exit(2)

    # Parse arguments.
    args = parser.parse_args()
    assert os.access(args.temp_path, os.W_OK)

    # Get chemistry defs
    (reference_dict, chemistry_def) = chemistry.setup_chemistry(args)

    # Load TAGs/ABs.
    ab_map = preprocessing.parse_tags_csv(args.tags)
    ordered_tags, longest_tag_len = preprocessing.check_tags(ab_map, args.max_error)

    # Identify input file(s)
    read1_paths, read2_paths = preprocessing.get_read_paths(
        args.read1_path, args.read2_path
    )
    # Checks before chunking.
    (n_reads, R2_min_length, maximum_distance) = preprocessing.pre_run_checks(
        read1_paths=read1_paths,
        chemistry_def=chemistry_def,
        longest_tag_len=longest_tag_len,
        args=args,
    )

    # Chunk the data to disk before mapping
    (
        input_queue,
        temp_files,
        R1_too_short,
        R2_too_short,
        total_reads,
    ) = io.write_chunks_to_disk(
        args=args,
        read1_paths=read1_paths,
        read2_paths=read2_paths,
        R2_min_length=R2_min_length,
        n_reads_per_chunk=n_reads,
        chemistry_def=chemistry_def,
        ordered_tags=ordered_tags,
        maximum_distance=maximum_distance,
    )
    # Map the data
    (
        final_results,
        umis_per_cell,
        reads_per_cell,
        merged_no_match,
    ) = processing.map_data(
        input_queue=input_queue, unmapped_id=len(ordered_tags), args=args
    )

    # Check if 99% of the reads are unmapped.
    processing.check_unmapped(
        no_match=merged_no_match,
        too_short=R1_too_short + R2_too_short,
        total_reads=total_reads,
        start_trim=chemistry_def.R2_trim_start,
    )

    # Remove temp chunks
    for file_path in temp_files:
        os.remove(file_path)

    # Correct cell barcodes
    if args.bc_threshold != 0:
        (
            final_results,
            umis_per_cell,
            bcs_corrected,
        ) = processing.run_cell_barcode_correction(
            final_results=final_results,
            umis_per_cell=umis_per_cell,
            reads_per_cell=reads_per_cell,
            reference_dict=reference_dict,
            ordered_tags=ordered_tags,
            args=args,
        )
    else:
        print("Skipping cell barcode correction")
        bcs_corrected = 0

    # If given, use reference_list for top cells
    top_cells_tuple = umis_per_cell.most_common(args.expected_cells * 10)
    if reference_dict:
        # Add potential missing cell barcodes.
        # for missing_cell in reference_list:
        #     if missing_cell in final_results:
        #         continue
        #     else:
        #         final_results[missing_cell] = dict()
        #         for TAG in ordered_tags:
        #             final_results[missing_cell][TAG.safe_name] = Counter()
        #         filtered_cells.add(missing_cell)
        top_cells = [pair[0] for pair in top_cells_tuple]
        filtered_cells = []
        for cell in top_cells:
            # pylint: disable=no-member
            if cell in reference_dict.keys():
                filtered_cells.append(cell)
    else:
        # Select top cells based on total umis per cell
        filtered_cells = [pair[0] for pair in top_cells_tuple]

    # Create sparse matrices for reads results
    read_results_matrix = processing.generate_sparse_matrices(
        final_results=final_results,
        ordered_tags=ordered_tags,
        filtered_cells=filtered_cells,
    )
    # Write reads to file
    io.write_to_files(
        sparse_matrix=read_results_matrix,
        filtered_cells=filtered_cells,
        ordered_tags=ordered_tags,
        data_type="read",
        outfolder=args.outfolder,
        reference_dict=reference_dict,
    )

    # UMI correction

    if args.umi_threshold != 0:
        # Correct UMIS
        (
            final_results,
            umis_corrected,
            clustered_cells,
        ) = processing.run_umi_correction(
            final_results=final_results,
            filtered_cells=filtered_cells,
            unmapped_id=len(ordered_tags),
            args=args,
        )
    else:
        # Don't correct
        umis_corrected = 0
        clustered_cells = []

    if len(clustered_cells) > 0:
        # Remove clustered cells from the top cells
        for cell_barcode in clustered_cells:
            filtered_cells.remove(cell_barcode)

        # Create sparse clustered cells matrix
        umi_clustered_matrix = processing.generate_sparse_matrices(
            final_results=final_results,
            ordered_tags=ordered_tags,
            filtered_cells=clustered_cells,
        )
        # Write uncorrected cells to dense output
        io.write_dense(
            sparse_matrix=umi_clustered_matrix,
            ordered_tags=ordered_tags,
            columns=clustered_cells,
            outfolder=os.path.join(args.outfolder, "uncorrected_cells"),
            filename="dense_umis.tsv",
        )
    # Generate the UMI count matrix
    umi_results_matrix = processing.generate_sparse_matrices(
        final_results=final_results,
        ordered_tags=ordered_tags,
        filtered_cells=filtered_cells,
        umi_counts=True,
    )

    # Write umis to file
    io.write_to_files(
        sparse_matrix=umi_results_matrix,
        filtered_cells=filtered_cells,
        ordered_tags=ordered_tags,
        data_type="umi",
        outfolder=args.outfolder,
        reference_dict=reference_dict,
    )

    # Write unmapped sequences
    if len(merged_no_match) > 0:
        io.write_unmapped(
            merged_no_match=merged_no_match,
            top_unknowns=args.unknowns_top,
            outfolder=args.outfolder,
            filename=args.unmapped_file,
        )

    # Create report and write it to disk
    io.create_report(
        total_reads=total_reads,
        reads_per_cell=reads_per_cell,
        no_match=merged_no_match,
        version=argsparser.get_package_version(),
        start_time=start_time,
        ordered_tags=ordered_tags,
        umis_corrected=umis_corrected,
        bcs_corrected=bcs_corrected,
        bad_cells=clustered_cells,
        R1_too_short=R1_too_short,
        R2_too_short=R2_too_short,
        args=args,
        chemistry_def=chemistry_def,
        maximum_distance=maximum_distance,
    )

    # Write dense matrix to disk if requested
    if args.dense:
        print("Writing dense format output")
        io.write_dense(
            sparse_matrix=umi_results_matrix,
            ordered_tags=ordered_tags,
            columns=filtered_cells,
            outfolder=args.outfolder,
            filename="dense_umis.tsv",
        )


if __name__ == "__main__":
    main()
