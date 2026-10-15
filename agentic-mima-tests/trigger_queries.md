# Triggering test set

Guide, Chapter 3: run 10 to 20 queries that should trigger the skill and count how many load it automatically (target about 90%). Also run queries that should not, and paraphrases.

How to record: start a new conversation for each query with the skill enabled, send only the query, and note whether the skill loaded (Claude uses its instructions or scripts) without being named. Do not mention "agentic-mima" in the query.

Debug an undertriggering result by asking Claude: "When would you use the agentic-mima skill?" Claude quotes the description back; add the missing words to the description.

## Should trigger (12)

| # | Query | Type | Loaded? |
|---|---|---|---|
| 1 | Run MIMA quality control on my shotgun reads, I have a manifest.csv | obvious | |
| 2 | Generate a taxonomic profile with Kraken2 and Bracken for these gut samples | obvious | |
| 3 | Compare the microbiome of healthy controls and patients with disease | intent only | |
| 4 | I have CleanReads from QC. How do I get functional pathways with HUMAnN3? | paraphrase | |
| 5 | Make genus bar plots of my Bracken results grouped by lab | paraphrase | |
| 6 | Compare alpha and beta diversity of control and disease and give me stats and plots | intent only | |
| 7 | Train a random forest on my feature tables to classify samples by Gender | paraphrase | |
| 8 | My Kraken2 PBS job was killed, why? | troubleshooting | |
| 9 | Submit the generated PBS scripts for the QC stage | submission | |
| 10 | What does corrected_sig mean in my diversity output? | interpretation | |
| 11 | Write up the results of my shotgun metagenomics run as a report | reporting | |
| 12 | Is my manifest.csv valid for the QC step? | validation | |

## Should not trigger (8)

| # | Query | Why | Loaded? |
|---|---|---|---|
| 1 | Analyse my 16S amplicon data with QIIME 2 | explicit negative trigger | |
| 2 | Assemble genomes from my metagenome reads and bin them with MetaBAT | explicit negative trigger | |
| 3 | Write a Python function to parse FASTQ files | general coding | |
| 4 | What is the weather in Sydney? | unrelated | |
| 5 | Do differential expression on my RNA-seq with DESeq2 | different pipeline | |
| 6 | Create an Excel sheet of my lab inventory | unrelated | |
| 7 | Submit my Slurm job for a genome alignment | different scheduler and pipeline | |
| 8 | Explain what the human microbiome is | general knowledge (borderline: record the result, no verdict) | |

## Scoring

- Should-trigger rate = loaded / 12. Target about 90%.
- Should-not-trigger: any load is a defect, except row 8, which is informational.
- For a should-not query that loads, record whether Claude then refused correctly. A refusal after loading is acceptable behaviour but is not the same as not triggering; report them separately.
