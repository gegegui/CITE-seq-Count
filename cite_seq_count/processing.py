import time
import gzip
import sys
import os
import Levenshtein
from collections import Counter
from itertools import islice
from numpy import int16
from scipy import sparse
from umi_tools import network

def classify_reads_multi_process(read1_path, read2_path, chunk_size,
                    tags, barcode_slice, umi_slice,
                    regex_pattern, first_line, whitelist,
                    legacy, debug):
    """Read through R1/R2 files and generate a islice starting at a specific index.

    It reads both Read1 and Read2 files, creating a dict based on cell barcode.

    Args:
        read_path1 (string): Path to R1.fastq.gz
        read_path2 (string): Path to R2.fastq.gz
        chunk_size (int): The number of lines to process 
        tags (dict): A dictionary with the TAGs + TAG Names.
        barcode_slice (slice): A slice for extracting the Barcode portion from the
            sequence.
        umi_slice (slice): A slice for extracting the UMI portion from the
            sequence.
        regex_pattern (regex.Pattern): An object that matches against any of the
            provided TAGs within the maximum distance provided.
        first_line (int): The first line to process in the file.
        whitelist (set): The set of white-listed barcodes.
        include_no_match (bool, optional): Whether to keep track of the
            `no_match` tags. Default is True.
        legacy (bool): If the tags contain a polyA or not.
        debug (bool): Print debug messages. Default is False.

    Returns:
        pandas.DataFrame: Matrix with the resulting counts.
        dict(int): A dictionary with the counts for each `no_match` TAG, based
            on the length of the longest provided TAG.

    """
    results_table = {}
    no_match_table = Counter()
    # Get the length of the longest TAG.
    longest_ab_tag = len(next(iter(tags)))
    n = 1
    t=time.time()
    if legacy:
        max_tag_length = longest_ab_tag + 6
    else:
        max_tag_length = longest_ab_tag
    with gzip.open(read1_path, 'rb') as textfile1, \
         gzip.open(read2_path, 'rb') as textfile2:
        
        # Read all 2nd lines from 4 line chunks. If first_n not None read only 4 times the given amount.
        secondlines = islice(zip(textfile1, textfile2), first_line, first_line + chunk_size - 1, 4)
        for read1, read2 in secondlines:
            if n % 1000000 == 0:
                print("Processed 1,000,000 reads in {:.4} secondes. Total "
                    "reads: {:,} in child {}".format(time.time() - t, n, os.getpid()))
                sys.stdout.flush()
                t = time.time()
            read1 = read1.strip()
            read2 = read2.strip()
            cell_barcode = read1[barcode_slice]
            if whitelist is not None:
                if cell_barcode not in whitelist:
                    continue
            if cell_barcode not in results_table:
                results_table[cell_barcode] = {}
            TAG_seq = read2
            UMI = read1[umi_slice]

            # Apply regex to Read2.
            match = regex_pattern.search(TAG_seq)
            if match:
                # If a match is found, keep only the matching portion.
                TAG_seq = match.group(0)
                if debug:
                    print(
                        "\nline:{0}\n"
                        "cell_barcode:{1}\tUMI:{2}\tTAG_seq:{3}\n"
                        "line length:{4}\tcell barcode length:{5}\tUMI length:{6}\tTAG sequence length:{7}"
                        .format(read1 + read2, cell_barcode, UMI, TAG_seq,
                                len(read1 + read2), len(cell_barcode), len(UMI), len(TAG_seq)
                        )
                    )
                    sys.stdout.flush()
                # Get the distance by adding up the errors found:
                #   substitutions, insertions and deletions.
                distance = sum(match.fuzzy_counts)
                # To get the matching TAG, compare `match` against each TAG.

                for tag, name in tags.items():
                    # This time, calculate the distance using the faster function
                    # `Levenshtein.distance` (which does the same). Thus, both
                    # determined distances should match.
                    if Levenshtein.distance(tag, TAG_seq) == distance:
                        #results_table[cell_barcode]['total_reads'][UMI] += 1
                        try:
                            results_table[cell_barcode][name][UMI] += 1
                        except:
                            results_table[cell_barcode][name] = Counter()
                            results_table[cell_barcode][name][UMI] += 1
                            #print('{}: mapped {}'.format(n,TAG_seq))
                        break
            else:
                # Bad structure
                #print('{}: bad construct {}'.format(n,TAG_seq))
                try:
                    results_table[cell_barcode]['unmapped'][UMI] += 1
                except:
                    results_table[cell_barcode]['unmapped'] = Counter()
                    results_table[cell_barcode]['unmapped'][UMI] += 1
                no_match_table[TAG_seq] += 1
            n += 1
    print("Counting done for process {}. Processed {:,} reads".format(os.getpid(), n-1))
    sys.stdout.flush()
    return(results_table, no_match_table, n - 1)

def merge_results(parallel_results):
    """Merge chunked results from parallel processing.

    Args:
        parallel_results (list): List of dict with mapping results.

    Returns:
        merged_results (dict): Results combined as a dict of dict of Counters
        umis_per_cell (Counter): Total umis per cell as a Counter
        merged_no_match (Counter): Unmapped tags as a Counter
    """
    
    merged_results = {}
    merged_no_match = Counter()
    umis_per_cell = Counter()
    reads_per_cell = Counter()
    total_reads = 0
    for chunk in parallel_results:
        mapped = chunk[0]
        unmapped = chunk[1]
        n_reads = chunk[2]
        for cell_barcode in mapped:
            if cell_barcode not in merged_results:
                merged_results[cell_barcode] = dict()
            for TAG in mapped[cell_barcode]:
                # Test the counter. If empty, returns false
                if mapped[cell_barcode][TAG]:
                    if TAG not in merged_results[cell_barcode]:
                        merged_results[cell_barcode][TAG] = Counter()
                    for UMI in mapped[cell_barcode][TAG]:
                        merged_results[cell_barcode][TAG][UMI] += mapped[cell_barcode][TAG][UMI]
                        if TAG != 'unmapped':
                            umis_per_cell[cell_barcode] += len(mapped[cell_barcode][TAG])
                            reads_per_cell[cell_barcode] += mapped[cell_barcode][TAG][UMI]
        merged_no_match.update(unmapped)
        total_reads += n_reads
    return(merged_results, umis_per_cell, reads_per_cell, merged_no_match, total_reads)


def correct_umis(final_results):
    for cell_barcode in final_results:
        for TAG in final_results[cell_barcode]:
            if len(final_results[cell_barcode][TAG]) > 1:
                UMIclusters = umi_clusters(
                    final_results[cell_barcode][TAG].keys(),
                    final_results[cell_barcode][TAG],
                    1)
                for umi_barcodes in UMIclusters:  # This is a list with the first element the dominant barcode
                    if len(umi_barcodes) > 2:
                        major_umi = umi_barcodes[0]
                        for minor_umi in umi_barcodes[1:]:  # Iterate over all barcodes in a cluster
                            temp = final_results[cell_barcode][TAG].pop(minor_umi)
                            final_results[cell_barcode][TAG][major_umi] += temp
    return(final_results)

def correct_cells(final_results, reads_per_cell, umis_per_cell):
    cell_clusterer = network.UMIClusterer()
    CBclusters = cell_clusterer(list(reads_per_cell.keys()), reads_per_cell, 1)
    for cell_barcodes in CBclusters:  # This is a list with the first element the dominant barcode
        if(len(cell_barcodes) > 1):
            major_barcode = cell_barcodes[0]
            for minor_barcode in cell_barcodes[1:]:  # Iterate over all barcodes in a cluster except first
                temp = final_results.pop(minor_barcode)
                for TAG in temp:
                    try:
                        final_results[major_barcode][TAG].update(temp[TAG])
                    except:
                        final_results[major_barcode][TAG] = {}
                        final_results[major_barcode][TAG].update(temp[TAG])
                umis_per_cell[major_barcode] += umis_per_cell[minor_barcode]
                del(umis_per_cell[minor_barcode])
                reads_per_cell[major_barcode] += reads_per_cell[minor_barcode]
                del(reads_per_cell[minor_barcode])
    return(final_results, umis_per_cell)

def generate_sparse_matrices(final_results, ordered_tags_map, top_cells):
    """
    Create two sparse matrices with umi and read counts.

    Args:
        final_results (dict): Results in a dict of dicts of Counters.
        ordered_tags_map (dict): Tags in order with indexes as values.

    Returns:
        umi_results_matrix (scipy.sparse.dok_matrix): UMI counts
        read_results_matrix (scipy.sparse.dok_matrix): Read counts

    """
    umi_results_matrix = sparse.dok_matrix((len(ordered_tags_map) ,len(top_cells)), dtype=int16)
    read_results_matrix = sparse.dok_matrix((len(ordered_tags_map) ,len(top_cells)), dtype=int16)
    for i,cell_barcode in enumerate(top_cells):
        for j,TAG in enumerate(final_results[cell_barcode]):
            if final_results[cell_barcode][TAG]:
                umi_results_matrix[ordered_tags_map[TAG],i] = len(final_results[cell_barcode][TAG])
                read_results_matrix[ordered_tags_map[TAG],i] = sum(final_results[cell_barcode][TAG].values())
    return(umi_results_matrix, read_results_matrix)

