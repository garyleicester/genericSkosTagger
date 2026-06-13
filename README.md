# genericSkosTagger

A small, single-file concept tagger for any SKOS thesaurus you can reach over SPARQL. Point it at an endpoint, give it a language tag, and it will pull every concept's labels and stand up a tiny HTTP API that tags free text against them. There's a self-contained HTML page that talks to that API so you can see it working in a browser.

It was built originally against the CABI Thesaurus (CABT) in PoolParty, but there's nothing CABI-specific in here - it's boiled down to a generic, 'use for anything' version. It will work against AGROVOC, the Getty vocabularies, your own Fuseki/GraphDB instance, or anything else that speaks SPARQL and models its terms in SKOS.

## What it does, in one breath

On startup it runs one SPARQL query to collect `skos:prefLabel` and `skos:altLabel` for every concept (optionally filtered to a scheme and/or a concept-IRI pattern), builds in-memory lookup tables, and serves a single `POST /extract_concepts` endpoint. You send it text, it returns the concepts it found, with notes about *how* each was matched and whether matches reinforced or collided with each other.

## A note on the resource files (please read this)

The original tool used two additional resource files that powered its smartest matching layer - a morphology table and a word-frequency list. Those files are not provided.

What I *have* done is document their formats exactly (see [The optional NLP layer](#the-optional-nlp-layer-and-its-two-files) below), so you can build or source your own equivalents and slot them in. The code is written so that **if you don't supply these files, the tagger runs perfectly well without them** - you just lose the morphological-variant matching layer (the "NLP" match type). Everything else works.

In other words: the useful, reusable, durable thing here is the *method* and the *file formats*. The exact contents of my lists were only ever one instance of those formats. You can make better ones, cleanly.

## Requirements

- Python 3.8+
- `pip install requests flask flask-cors`
- A reachable SPARQL endpoint whose vocabulary uses SKOS labels

## Quick start

Tag against a public endpoint - AGROVOC needs no auth:

```
python genericSkosTagger.py --endpoint https://agrovoc.fao.org/sparql --lang en
```

Then open `genericSkosTagger.html` in a browser. The endpoint box defaults to `http://localhost:5000/extract_concepts`; paste some text, hit the button, and you'll see tagged concepts and the raw JSON.

Or hit the API directly:

```
curl -X POST -H "Content-Type: application/json" \
  --data '{"text":"maize and natural enemies in low income countries"}' \
  http://localhost:5000/extract_concepts
```

## Running against an authenticated endpoint

The PoolParty-style invocation, with basic auth and scheme/IRI filtering:

```
python genericSkosTagger.py \
  --endpoint https://<ADDRESS>/PoolParty/sparql/<PROJECT> \
  --username 'user' --password 'pass' \
  --lang en-GB \
  --scheme-uri https://<ADDRESS>/<PROJECT>/<SCHEME_IDENTIFIER>  \
  --concept-uri-regex "/[0-9]+?$"
```

Credentials can also come from the environment (`SPARQL_USERNAME`, `SPARQL_PASSWORD`) so you don't have to put them on the command line or in your shell history.

## All the options

| Flag | Purpose |
|------|---------|
| `--endpoint` | **Required.** SPARQL endpoint URL. |
| `--username` / `--password` | Basic-auth credentials. Default to env vars `SPARQL_USERNAME` / `SPARQL_PASSWORD`. Omit both for open endpoints. |
| `--lang` | Language tag to filter labels on, e.g. `en`, `en-GB`, `es`. Default `en`. Must match the tags actually used in the data. |
| `--scheme-uri` | Restrict to concepts in exactly this `skos:inScheme`. |
| `--scheme-regex` | Alternative to the above: restrict to schemes whose IRI matches this regex. (Use one or the other, not both.) |
| `--concept-uri-regex` | Only keep concepts whose IRI matches this regex. Handy for excluding metadata concepts - e.g. `"/[0-9]+?$"` keeps only numeric-IDed concepts. |
| `--nlp-labels` | Path to a pre-built TSV of extra labels (see below). An alternative to generating them at runtime. |
| `--word-freq` | Path to a word-frequency file. Part of the optional NLP layer. |
| `--morph` | Path to a morphology-substitution file. Part of the optional NLP layer. |
| `--host` | Bind address. Default `0.0.0.0`. |
| `--port` | Port. Default `5000`. |
| `--debug` | Run Flask in debug mode. |

## How matching works

When text comes in, it's split on whitespace into words. The tagger then slides a window over those words, **longest phrases first**, down to single words. The window length starts at the longest label it saw in the data (computed automatically), so multi-word concepts get a fair chance before their component words are consumed. Once a span of words is claimed by a match, those words are marked consumed and won't be re-matched by a shorter window. This is a greedy, longest-match-wins strategy - simple, fast, and predictable.

Each candidate span is checked against the lookup tables in this order, and the first hit wins:

1. **`prefLabel`** - exact, case-sensitive match against a preferred label.
2. **`synonym`** - exact, case-sensitive match against an alternative label (`skos:altLabel`).
3. **`fuzzy`** - match after "selective lowercasing" (words with two or more capitals, like acronyms, are left alone; everything else is lowercased). This catches case differences without flattening `WHO` into `who`.
4. **`NLP`** - match against the generated/extrapolated label store, if you've enabled the optional layer. This is where morphological variants live.

The match type is reported back in the JSON as `Match_type`, so a downstream consumer can decide how much to trust each one (e.g. accept `prefLabel`/`synonym` automatically, route `fuzzy`/`NLP` to a human).

### The notes you get back

After matching, two passes annotate the results:

- **`homograph`** - the same surface string matched more than one concept (e.g. a name that's both a place and a person). Every result sharing that surface string gets the note.
- **`reinforced:N`** - the same concept URI was matched `N` times across the text. A rough confidence signal: a concept mentioned repeatedly is more likely to be genuinely on-topic.

### Example response

```json
[
  {
    "URI": "https://id.cabi.org/cabt_stable/348352",
    "prefLabel": "low income countries",
    "Match_type": "prefLabel",
    "Match_label": "low income countries",
    "Match_notes": []
  },
  {
    "URI": "https://id.cabi.org/cabt_stable/127140",
    "prefLabel": "Zea mays",
    "Match_type": "synonym",
    "Match_label": "maize",
    "Match_notes": ["reinforced:2"]
  }
]
```

## The optional NLP layer, and its two files

This is the part the missing resource files powered. When you supply **both** `--word-freq` and `--morph`, the tagger does an extra step at startup: for every concept, it takes the labels, generates plausible morphological variants of them, filters those variants, and adds the survivors to the `NLP` match store. The effect is that text like "arts activities" can match a concept labelled "art activity", or a label phrased one way can be reached from text phrased another.

It runs inside a try/except - if anything about these files is wrong, you get a warning and the tagger carries on without the NLP layer rather than failing.

Below are the exact formats the loader expects. Build your own from openly-licensed sources (some pointers at the end).

### `--morph` - the morphology substitution table

Tab-separated, one entry per line:

```
look-up-token<TAB>[variant1|variant2|variant3]
```

- **Left column:** a single token to look for. Tokens are matched after a label is lowercased and split on spaces, so entries should be lowercase single words (hyphenated words count as one token).
- **Right column:** a bracketed, pipe-separated list of replacement forms. The brackets and pipes are required syntax, not decoration.

Real examples in the format the loader expects:

```
monopolizer	[monopolizer|monopolizers|monopoliser|monopolisers]
monopolizers	[monopolizer|monopolizers|monopoliser|monopolisers]
monopolizes	[monopolize|monopolizes|monopolized|monopolizing|monopolise|monopolises|monopolised|monopolising]
monopolizing	[monopolize|monopolizes|monopolized|monopolizing|monopolise|monopolises|monopolised|monopolising]
monopoly	[monopoly|monopolies]
```

How it's used: each label is lowercased, spaces become `#`, and the result is wrapped in `#…#` so tokens are cleanly delimited (`#pregnant#women#`). For each token that appears as a left-column key, the **first** occurrence is substituted with its bracket group. The bracketed groups are then expanded combinatorially into concrete variant strings by `_expand_variants` - `[woman|women]` becomes two candidate labels, one with each form. Nested or multiple bracket groups expand recursively.

This is the larger of the two files in practice and does the most work. A good morphology table maps each inflected form back to its lemma *and* forward to its full inflectional family, so the expander can reach any surface form from any other.

### `--word-freq` - the word-frequency veto list

Tab-separated, `word<TAB>integer`:

```
'arf	3
maize	5842
the	1000000
```

- **Left column:** a word, lowercased on load.
- **Right column:** a frequency score (an integer). If it's missing or non-numeric it's treated as `0`. If the same word appears more than once, the **highest** score is kept.

How it's used: it's a veto. After variant expansion, any generated variant whose frequency score is **greater than 6** is thrown away. The logic is that very common words make terrible discriminating labels - if a morphological variant collapses a concept down to an everyday word, you don't want it polluting your matches. The threshold of 6 is hard-coded in `_build_nlp_labels_from_bindings`; tune it there if your frequency scale is different from the one this was built against.

The absolute scale doesn't matter as long as it's consistent - what matters is that genuinely common words score above the threshold and rare/technical terms score below it. If you source a frequency list with a wildly different scale, adjust either the scores or that `> 6` test.

### Pre-building labels instead (`--nlp-labels`)

If you'd rather not regenerate variants on every startup - generation over a large vocabulary can be slow - you can compute them once and load them from a file with `--nlp-labels`. That file is a simpler TSV:

```
<concept-URI><TAB><label#label#label…>
```

One URI per line, labels lowercased and joined with `#`. These go straight into the `NLP` store with no morphology processing. This is the fast path for production.

## Where to source clean data for the two files

- **Frequency lists:** SUBTLEX (word frequencies from subtitle corpora) and the various openly-licensed n-gram frequency lists are good starting points. Pick one, then calibrate the `> 6` veto to its scale.
- **Morphology / inflections:** SCOWL and its associated AGID (Automatically Generated Inflection Database) give you lemma-to-inflection mappings under a permissive licence, which is close to what the morph table wants - you'll need to reshape them into the `token<TAB>[a|b|c]` format.

None of these will be identical to what I used, and that's fine - the formats are what's documented here, and well-sourced inputs in the right shape will give you a working, defensible NLP layer.

## The HTML client

`genericSkosTagger.html` is a standalone page - no build step, no dependencies. Open it in a browser, set the endpoint if it isn't the default `http://localhost:5000/extract_concepts`, paste text, and it renders the matched concepts as chips plus the raw JSON. It's there to demonstrate the API and to be a useful little tool in its own right; lift it, restyle it, embed it, whatever you like.

## Production notes

This ships with Flask's development server, which is fine for local use and demos but not for production traffic. For anything serious, put it behind a real WSGI server (gunicorn, uWSGI) and a reverse proxy. It has previously been reshaped as a FastAPI re-implementation, tuned for concurrency and throughput - the code here is the readable, single-file reference version, optimised for being understood and adapted rather than for raw speed. Though - as it's in memory - it's very fast as is.

## Licence

The code is yours to use, adapt, and build on. The resource files discussed above are explicitly **not** part of this repository and are not covered by it; you are expected to source and use your own, if the NLP layer is required.

## Motivation

Released generically in the hope it's useful to anyone working with SKOS vocabularies.
