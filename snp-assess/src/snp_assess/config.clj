;; Adjustable filtering and analysis parameters
;;
;; * kmer-range -> Range of values for kmers to normalize around. Lower
;;   kmer values are indicative of rare regions that are likely due to
;;   mutation.
;; * qual-range -> Range of read quality scores for normalization.
;; * map-score-range -> Range of aligner mapping scores.
;; * min-freq -> Bases below this frequency at a position are not
;;   called.
;; * allowed-freq-diff -> Difference allowed between expected and
;;   actual frequency to count as correct call.
;; * max-pos-pct -> Maximum minority variants to include in positive
;;   classification group.
;; * max-neg-pct -> Maximum minority variants to include in negative
;;   classification group.
;; * assess-bases -> Bases to perform assessment with a
;;   classifier. Set to nil to include all bases.
(ns snp-assess.config)

(def default-config
  {:kmer-range [1e-5 1e-1]
   :qual-range [4.0 35.0]
   :map-score-range [0.0 250.0]
   :random-coverage-step 100
   :random-coverage-sample 50
   :min-score 1.2
   :min-freq 0.0035
   :allowed-freq-diff 2.0
   :verbose false
   :classification {:max-pos-pct 10.0
                    :max-neg-pct 0.75
                    :naive-min-score 0.7
                    :assess-bases [790 3758]
                    :pass-thresh 0.5}})
