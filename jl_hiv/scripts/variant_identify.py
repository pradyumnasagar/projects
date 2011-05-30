#!/usr/bin/env python
"""Identify variants in barcoded multiplexed HIV samples.

Usage:
  variant_identify.py <YAML config>
"""
import os
import sys
import csv
import glob
import copy
import operator
import subprocess
import collections
import itertools
from contextlib import closing

import yaml
import pysam
import khmer
from Bio.SeqIO.QualityIO import (FastqGeneralIterator,
                                 _phred_to_sanger_quality_str)
from Bio.Seq import Seq

from bcbio.utils import create_dirs, map_wrap, cpmap
from bcbio.broad import BroadRunner
from bcbio.fastq.barcode import demultiplex, convert_illumina_oldstyle
from bcbio.fastq.unique import uniquify_bioplayground
from bcbio.fastq.trim import trim_fastq
from bcbio.fastq.filter import kmer_filter, remove_ns
from bcbio.ngsalign import novoalign
from bcbio.variation import mixed

def main(config_file):
    with open(config_file) as in_handle:
        config = yaml.load(in_handle)
    ref_index = novoalign.refindex(config["ref"], kmer_size=13, step_size=1)
    create_dirs(config)
    for cur in config["input"]:
        in_fastq = cur["fastq"]
        if cur.get("old_style_barcodes", False):
            in_fastq = convert_illumina_oldstyle(in_fastq)
        bc_files = demultiplex(in_fastq, cur["barcodes"],
                               config["dir"]["tmp"], config)
        with cpmap(config["algorithm"]["cores"]) as cur_map:
            for _ in cur_map(process_fastq, ((bc_file, ref_index, cur, config, config_file)
                                             for bc_file in bc_files)):
                pass

@map_wrap
def process_fastq(in_file, ref_index, cur_config, config, config_file):
    do_realignment = config["algorithm"].get("realignment", "")
    do_kmercorrect = config["algorithm"].get("kmer_correct", "")
    trim_three = config["algorithm"].get("trim_three", "")
    picard = BroadRunner(config["program"]["picard"], config["program"]["gatk"],
                         config["algorithm"]["java_memory"])
    if trim_three:
        in_file = trim_fastq(in_file, three=int(trim_three))
    if do_kmercorrect:
        in_file = remove_ns(in_file)
        in_file = kmer_filter(in_file, do_kmercorrect, config)
    unique_file = uniquify_bioplayground(in_file, config)
    align_sam = novoalign.align(config["dir"]["align"], ref_index, unique_file,
                                qual_format=cur_config.get("format", None))
    align_bam = to_bamsort(align_sam, unique_file, config, config_file)
    if do_realignment == "gatk":
        picard.run_fn("picard_index", align_bam)
        align_bam = picard.run_fn("gatk_realigner", align_bam, config["ref"],
                                  deep_coverage=True)
    picard.run_fn("picard_index", align_bam)
    if config["algorithm"].get("range_params", None):
        call_analyze_multiple(align_bam, in_file, config)
    else:
        call_bases_and_analyze(align_bam, in_file, config)

def call_analyze_multiple(align_bam, in_file, config):
    """Write output from multiple parameter settings in YAML format.

    This sets up an output file with the raw data for post-processing
    analysis.
    """
    call_stats = []
    for cur_params in apply(itertools.product,
                            [config["algorithm"][p]
                             for p in config["algorithm"]["range_params"]]):
        cur_config = copy.deepcopy(config)
        for name, val in zip(config["algorithm"]["range_params"], cur_params):
            cur_config["algorithm"][name] = val
        stats = call_bases_and_analyze(align_bam, in_file, cur_config, memoize=False)
        call_stats.extend(stats)
    out_file = os.path.join(config["dir"]["stats"], "%s.yaml" %
                            os.path.splitext(os.path.basename(in_file))[0])
    with open(out_file, "w") as out_handle:
        yaml.dump(call_stats, out_handle)

def call_bases_and_analyze(align_bam, in_file, config, memoize=True):
    out = []
    call_file, params = position_percent_file(align_bam, in_file, config, memoize)
    for expect in config["expected"]:
        out_info = {"file": align_bam, "region": expect["name"], "calls": []}
        out_info.update(params)
        counts = mixed.compare_files(call_file, expect["file"],
                                     expect["offset"], True)
        _print_expect_info(expect["name"], counts)
        for percent, vals in counts.items():
            vals["percent"] = percent
            out_info["calls"].append(vals)
        out.append(out_info)
    return out

def _print_expect_info(name, counts):
    print "** %s" % name
    percents = sorted(counts.keys(), reverse=True)
    print "| Percent | Correct | Wrong (partial) | Wrong |"
    print "|---------+---------+-----------------+-------|"
    for percent in percents:
        print "| % 7s | % 7s | % 15s | % 5s |" % (percent,
                                                  counts[percent].get("correct", 0),
                                                  counts[percent].get("partial", 0),
                                                  counts[percent].get("wrong", 0))

def position_percent_file(align_bam, read_file, config, memoize=True):
    kmer_size = config["algorithm"]["kmer_size"]
    min_thresh = config["algorithm"]["detection_thresh"]
    min_qual = int(config["algorithm"]["min_qual"])
    params = {"kmer": min_thresh, "qual": min_qual}
    print align_bam, params
    bases = ["A", "C", "G", "T"]
    out_file = os.path.join(config["dir"]["vrn"], "%s-variations.tsv" %
                            os.path.splitext(os.path.basename(align_bam))[0])
    if not memoize or not os.path.exists(out_file):
        with open(out_file, "w") as out_handle:
            writer = csv.writer(out_handle, dialect="excel-tab")
            writer.writerow(["space", "pos"] + bases)
            ktable, read_counts = count_kmers_and_reads(read_file, kmer_size)
            for chrom, pos, kmers in positional_kmers(align_bam, kmer_size, min_qual):
                base_percents = {}
                for base, percent in base_kmer_percents(kmers, ktable, read_counts,
                                                        min_thresh):
                    base_percents[base] = "%.1f" % (percent * 100.0)
                writer.writerow([chrom, pos] + [base_percents.get(b, "") for b in bases])
    return out_file, params

def base_kmer_percents(kmers, ktable, read_counts, min_thresh):
    """Retrieve percentages of each base call based on k-mer counts.
    """
    kmer_counts = []
    for kmer in list(set(k[0] for k in kmers)):
        kmer_counts.append(ktable.get(kmer))
    total = float(sum(kmer_counts))
    base_counts = collections.defaultdict(int)
    for (kmer, base, orig_seq), kcount in zip(kmers, kmer_counts):
        if total > 0 and kcount / total > min_thresh:
            base_counts[base] += read_counts[orig_seq]
    pass_total = float(sum(base_counts.values()))
    final = []
    for base, count in base_counts.iteritems():
        final.append((base, count / pass_total))
    final.sort(key=operator.itemgetter(1), reverse=True)
    return final

def positional_kmers(in_bam, kmer_size, min_qual):
    """Retrieve informative kmers at each piled up position in an alignment.
    """
    qual_map = {}
    for k, v in _phred_to_sanger_quality_str.iteritems():
        qual_map[v] = k
    with closing(pysam.Samfile(in_bam, 'rb')) as work_bam:
        for col in work_bam.pileup():
            space = work_bam.getrname(col.tid)
            kmers = list(set(filter(lambda x: x is not None,
                                    [_read_surround_region(r, kmer_size, min_qual, qual_map)
                                     for r in col.pileups])))
            yield space, col.pos, kmers

def _read_surround_region(read, kmer_size, min_qual, qual_map):
    """Provide context for an aligned read at a particular position.

    Requires a full length kmer at each side so excludes information near
    start and end of reads.
    """
    assert kmer_size % 2 == 1, "Need odd kmer size"
    extend = (kmer_size - 1) // 2
    if read.indel == 0:
        seq = read.alignment.seq
        if read.qpos >= extend and read.qpos < len(seq) - extend:
            qual = qual_map[read.alignment.qual[read.qpos]]
            if qual >= min_qual:
                kmer = seq[read.qpos-extend:read.qpos+extend+1]
                assert len(kmer) == kmer_size, (kmer, seq, read.qpos, len(seq))
                call = seq[read.qpos]
                if read.alignment.is_reverse:
                    seq = str(Seq(seq).reverse_complement())
                return (kmer, call, seq)

def count_kmers_and_reads(in_fastq, kmer_size):
    ktable = khmer.new_ktable(kmer_size)
    read_count = collections.defaultdict(int)
    with open(in_fastq) as in_handle:
        i = 0
        for (_, seq, _) in FastqGeneralIterator(in_handle):
            i += 1
            #if i > 1e5: break
            if seq.find("N") == -1:
                ktable.consume(seq)
                read_count[seq] += 1
    return ktable, dict(read_count)

def to_bamsort(sam_file, fastq_file, config, config_file):
    sample_name = os.path.splitext(os.path.basename(sam_file))[0]
    cl = [config["program"]["bamsort"], "--name=%s" % sample_name,
          config_file, sam_file, config["ref"], fastq_file]
    subprocess.check_call(cl)
    return glob.glob("%s*-sort.bam" % os.path.splitext(sam_file)[0])[0]

if __name__ == "__main__":
    main(*sys.argv[1:])