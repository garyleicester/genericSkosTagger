import argparse
import requests
from collections import defaultdict
import string
import json
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import math
import re
import os

# --- Flask app ---
app = Flask(__name__)
CORS(app)

# --- Global (populated from CLI args in main) ---
endpoint_url = None
username = None
password = None
lang = "en"  # default
scheme_uri = None
scheme_regex = None
nlp_labels_path = None
concept_uri_regex = None
word_freq_path = None
morph_bits_path = None

# --- Stores ---
store_pref = {}                # lowercase version of prefLabels (for fuzzy)
store_pref_case = {}           # exact case version of prefLabels (for exact match)
store_alt = defaultdict(set)   # exact-case alt labels
store_norm = defaultdict(set)  # normalized (lowercased selectively) labels
pref_by_uri = {}

store_nlp = defaultdict(set)       # additional labels from file (optional)
max_chunk_size = 1

# --- Helpers ---
def execute_sparql_query(sparql_query: str):
    payload = {
        "query": sparql_query,
        "content-type": "application/json",
    }
    headers = {
        "Accept": "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    response = requests.post(
        endpoint_url,
        auth=(username, password) if username or password else None,
        headers=headers,
        data=payload,
        timeout=60,
    )
    response.encoding = "utf-8"
    response.raise_for_status()
    return response.json()

def _sq(s: str) -> str:
    """Escape backslashes and single quotes for use inside single-quoted SPARQL strings."""
    return s.replace("\\", "\\\\").replace("'", "\\'")

def build_thesaurus_query():
    """Build a generic SKOS query with optional scheme filter and concept-IRI regex."""
    prefixes = [
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>",
    ]

    scheme_lines = []
    if scheme_uri:
        scheme_lines.append(f"?concept skos:inScheme <{scheme_uri}> .")
    elif scheme_regex:
        scheme_lines.append("?concept skos:inScheme ?scheme .")
        scheme_lines.append(f"FILTER(REGEX(STR(?scheme), '{_sq(scheme_regex)}'))")

    concept_filter = f"FILTER(REGEX(STR(?concept), '{_sq(concept_uri_regex)}'))" if concept_uri_regex else ""

    where_lines = [
        "?concept skos:prefLabel ?prefLabel .",
        f"FILTER(LANG(?prefLabel) = '{lang}')",
        *scheme_lines,
        concept_filter,
        f"OPTIONAL {{ ?concept skos:altLabel ?altLabel . FILTER(LANG(?altLabel) = '{lang}') }}",
    ]

    select_bits = [
        "?concept",
        "?prefLabel",
        "(GROUP_CONCAT(DISTINCT ?altLabel; SEPARATOR = '#') AS ?altLabels)",
    ]
    group_by_bits = ["?concept", "?prefLabel"]

    prefix_str = "\n".join(prefixes)
    where_str = "\n".join([line for line in where_lines if line])
    select_str = " ".join(select_bits)
    group_by_str = " ".join(group_by_bits)

    query = (
        f"{prefix_str}\n"
        f"SELECT DISTINCT {select_str}\n"
        "WHERE {\n"
        f"      {where_str}\n"
        "}\n"
        f"GROUP BY {group_by_str}"
    )
    return query

def warn(msg: str) -> None:
    print(msg, flush=True)

def _load_freqscores(path: str):
    warn("reading word frequency data")
    freq = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            # tabs: word\tfreq
            parts = raw.rstrip("\r\n").split("\t")
            if not parts or not parts[0]:
                continue
            word = parts[0].lower()
            try:
                val = int(parts[1]) if len(parts) > 1 and re.search(r"[0-9]", parts[1] or "") else 0
            except ValueError:
                val = 0
            existing = freq.get(word)
            if not (existing and (existing > val)):  # keep MAX (Perl logic)
                freq[word] = val
    warn("done (freq)")
    return freq

def _load_morph_bits(path: str):
    warn("reading morphology substitutions")
    lookup = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\r\n").strip()
            tabs = line.split("\t")
            look = tabs[0] if len(tabs) > 0 else ""
            result = tabs[1] if len(tabs) > 1 else ""
            if look and result:
                lookup[look] = result
    warn("done (morph)")
    return lookup

def _expand_variants(text: str):
    """
    Expand first [a|b] group recursively (Perl logic), return unique list preserving order.
    """
    m = re.search(r"\[([^\]]+?)\]", text or "")
    if not m:
        return [text]
    group = m.group(1)
    variants = group.split("|")
    out = []
    seen = set()
    for v in variants:
        new_text = re.sub(re.escape(f"[{group}]"), v, text, count=1)
        for e in _expand_variants(new_text):
            if e not in seen:
                seen.add(e)
                out.append(e)
    return out

def _build_nlp_labels_from_bindings(bindings, freq, morph):
    """
    For each concept, take prefLabel + altLabels, generate extrapolated labels (DE branch only),
    and store in store_nlp[lbl].add(uri).
    """
    added = 0

    for b in bindings:
        uri = b.get("concept", {}).get("value", "")
        pref = b.get("prefLabel", {}).get("value", "")
        alt_field = b.get("altLabels", {}).get("value", "")
        if not uri:
            continue

        labels = []
        if alt_field:
            labels.extend(alt_field.split("#"))
        if pref:
            labels.append(pref)

        # Build set to avoid duplicates (case-insensitive check later)
        label_lookup = { (lbl or "").lower(): 1 for lbl in labels }

        extrapolated_here = []

        for label in labels:
            if not label:
                continue
            # Skip some specific forms (kept from Perl; trivial example retained)
            if re.match(r"^(blacks|whites)$", label or ""):
                continue

            # Skip tokens with two capitals and no space/hyphen (e.g. “iOS14”)
            if (not re.search(r" ", label or "")) and (not re.search(r"-", label or "")) and re.search(r"[A-Z].*?[A-Z]", label or ""):
                continue

            label_in = label or ""
            label_in_space_count = len(re.findall(r"( )", label_in))

            # Token substitution using morph table:
            # 1) lowercase, 2) convert spaces to #, 3) wrap with #...#
            label_mod = (label or "").lower().replace(" ", "#")
            label_mod = f"#{label_mod}#"
            tokens = label_mod.split("#")

            # Replace first occurrence per token (Perl behavior)
            for token in tokens:
                if token and token in morph:
                    repl = morph[token]
                    label_mod = re.sub(r"#" + re.escape(token) + r"#", "#" + repl + "#", label_mod, count=1)

            # Back to spaces + trim
            label_mod = label_mod.replace("#", " ").strip()

            # Expand bracketed variants
            variants = _expand_variants(label_mod)

            # Filter variants using Perl’s quirky rules
            filtered = list(variants)
            for variant in variants:
                new_space_count = len(re.findall(r"( )", variant))
                # Perl’s !~ number-as-string match quirk
                if not re.search(str(label_in_space_count), str(new_space_count)):
                    advice = "REJECT"
                else:
                    advice = "KEEP"
                # Frequency veto
                if freq.get(variant, 0) > 6:
                    advice = "REJECT"
                if advice == "REJECT":
                    filtered = [x for x in filtered if not re.fullmatch(re.escape(variant), x)]

            filtered = [x for x in filtered if x != ""]
            extrapolated_here.extend(filtered)

        # uniq, then drop ones already present in original label list (case-insensitive)
        seen = set()
        uniqed = []
        for x in extrapolated_here:
            if x not in seen:
                seen.add(x)
                uniqed.append(x)

        final_labels = [x for x in uniqed if x.lower() not in label_lookup]

        # Perl prints only if dedup removed something AND at least one remains.
        # Here: we just keep whatever remains (pragmatic for NLP store).
        for lbl in final_labels:
            lbl = lbl.strip()
            if lbl:
                store_nlp.setdefault(lbl, set()).add(uri)
                added += 1

    warn(f"NLP generation added {added} label→URI mappings")

def load_thesaurus_data():
    print("Executing SPARQL query for thesaurus data…")
    sparql_query = build_thesaurus_query()
    results = execute_sparql_query(sparql_query)
    print("SPARQL query executed successfully.")
    # Keep bindings for NLP build
    bindings = results.get("results", {}).get("bindings", [])
    process_sparql_results(results)
    print("Thesaurus data loaded.")

    # Optional: build NLP labels from the same concept set
    if word_freq_path and morph_bits_path:
        try:
            freq = _load_freqscores(word_freq_path)
            morph = _load_morph_bits(morph_bits_path)
            _build_nlp_labels_from_bindings(bindings, freq, morph)
        except Exception as e:
            warn(f"Failed to build NLP labels: {e}")

def process_sparql_results(results):
    global max_chunk_size
    bindings = results.get("results", {}).get("bindings", [])
    for b in bindings:
        uri = b.get("concept", {}).get("value", "")
        pref = b.get("prefLabel", {}).get("value", "")
        alt_labels_field = b.get("altLabels", {}).get("value", "")

        alt_labels = alt_labels_field.split("#") if alt_labels_field else []

        if pref and uri:
            store_pref_case.setdefault(pref, set()).add(uri)
            pref_by_uri[uri] = pref
            store_pref.setdefault(pref.lower(), set()).add(uri)

        for label in alt_labels:
            if not label:
                continue
            store_alt.setdefault(label, set()).add(uri)
            if not re.search(r"[A-Z]{2,}", label):
                store_norm.setdefault(label.lower(), set()).add(uri)

        if pref and not re.search(r"[A-Z]{2,}", pref):
            store_norm.setdefault(pref.lower(), set()).add(uri)

    max_chunk_size = max((label.count(" ") + 1 for label in store_norm.keys()), default=1)
    print(f"Max chunk size dynamically set to: {max_chunk_size}")


def load_nlp_labels():
    global store_nlp
    if not nlp_labels_path:
        print("No NLP labels file provided. Skipping.")
        return
    if os.path.exists(nlp_labels_path):
        print("Loading NLP labels from:", nlp_labels_path)
        with open(nlp_labels_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) != 2:
                    continue
                uri, labels_str = parts
                for lbl in labels_str.split("#"):
                    lbl = lbl.strip()
                    if lbl:
                        store_nlp.setdefault(lbl, set()).add(uri)
        print("NLP labels loaded.")
    else:
        print("NLP label file not found; skipping.")


# --- Matching ---

def selective_lowercase(text: str) -> str:
    return " ".join(
        word if re.search(r"[A-Z]{2,}", word) else word.lower()
        for word in text.split()
    )


def find_concepts(text: str):
    concepts = []
    words = text.split()
    consumed = [False] * len(words)

    for size in range(max_chunk_size, 0, -1):
        i = 0
        while i < len(words):
            segment = []
            idxs = []
            k = i
            while k < len(words) and len(segment) < size:
                if not consumed[k]:
                    segment.append(words[k])
                    idxs.append(k)
                k += 1
            if len(segment) < size:
                i += 1
                continue

            chunk_str = " ".join(segment).strip(string.punctuation)
            chunk_clean = selective_lowercase(chunk_str)

            # prefLabel (case-sensitive)
            if chunk_str in store_pref_case:
                uris = store_pref_case[chunk_str]
                for idxc in idxs:
                    consumed[idxc] = True
                for uri in uris:
                    concepts.append({
                        "URI": uri,
                        "prefLabel": pref_by_uri.get(uri, "Unknown"),
                        "Match_type": "prefLabel",
                        "Match_label": chunk_str,
                        "Match_notes": [],
                    })
                i += 1
                continue

            # altLabel (exact-case)
            if chunk_str in store_alt:
                uris = store_alt[chunk_str]
                for idxc in idxs:
                    consumed[idxc] = True
                for uri in uris:
                    concepts.append({
                        "URI": uri,
                        "prefLabel": pref_by_uri.get(uri, "Unknown"),
                        "Match_type": "synonym",
                        "Match_label": chunk_str,
                        "Match_notes": [],
                    })
                i += 1
                continue

            # fuzzy (normalized)
            if chunk_clean in store_norm:
                uris = store_norm[chunk_clean]
                for idxc in idxs:
                    consumed[idxc] = True
                for uri in uris:
                    concepts.append({
                        "URI": uri,
                        "prefLabel": pref_by_uri.get(uri, "Unknown"),
                        "Match_type": "fuzzy",
                        "Match_label": chunk_str,
                        "Match_notes": [],
                    })
                i += 1
                continue

            # NLP labels (optional)
            if chunk_clean in store_nlp:
                uris = store_nlp[chunk_clean]
                for idxc in idxs:
                    consumed[idxc] = True
                for uri in uris:
                    concepts.append({
                        "URI": uri,
                        "prefLabel": pref_by_uri.get(uri, "Unknown"),
                        "Match_type": "NLP",
                        "Match_label": chunk_str,
                        "Match_notes": [],
                    })
                i += 1
                continue

            i += 1

    return concepts


def analyse_concepts_json(results):
    # 1) homograph notes (same surface label -> multiple URIs)
    label_map = defaultdict(list)
    for i, c in enumerate(results):
        c["Match_notes"] = list(c["Match_notes"])  # ensure list
        label_map[c["Match_label"]].append(i)
    for lbl, idxs in label_map.items():
        uris = {results[idx]["URI"] for idx in idxs}
        if len(uris) > 1:
            for idx in idxs:
                if "homograph" not in results[idx]["Match_notes"]:
                    results[idx]["Match_notes"].append("homograph")

    # 2) reinforcement (same URI appears multiple times)
    uri_map = defaultdict(list)
    for i, c in enumerate(results):
        uri_map[c["URI"]].append(i)
    for uri, idxs in uri_map.items():
        count = len(idxs)
        if count > 1:
            note = f"reinforced:{count}"
            for idx in idxs:
                if note not in results[idx]["Match_notes"]:
                    results[idx]["Match_notes"].append(note)

    return results


# --- API ---
@app.route('/extract_concepts', methods=['POST'])
def extract_concepts():
    try:
        if request.is_json:
            data = request.get_json()
            text = data.get('text', '')
        else:
            text = request.form.get('text', '')
        if not text:
            return jsonify({"error": "No text provided"}), 400

        results = find_concepts(text)
        analyse_concepts_json(results)

        # Serialize results with ensure_ascii=False
        json_results = json.dumps(results, ensure_ascii=False)
        return Response(json_results, content_type='application/json; charset=utf-8')
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- CLI + bootstrap ---
def parse_args():
    p = argparse.ArgumentParser(description="Generic SKOS concept extractor API (only /extract_concepts)")
    p.add_argument('--endpoint', required=True, help='SPARQL endpoint URL')
    p.add_argument('--username', default=os.getenv('SPARQL_USERNAME', ''), help='SPARQL basic auth username (or env SPARQL_USERNAME)')
    p.add_argument('--password', default=os.getenv('SPARQL_PASSWORD', ''), help='SPARQL basic auth password (or env SPARQL_PASSWORD)')
    p.add_argument('--lang', default=os.getenv('SKOS_LANG', 'en'), help='Language tag to filter labels (default: en)')
    p.add_argument('--scheme-uri', help='Restrict to this skos:inScheme URI')
    p.add_argument('--scheme-regex', help='Restrict to schemes whose IRI matches this regex (applies to STR(?scheme))')
    p.add_argument('--nlp-labels', help='Path to optional TSV file: <URI>\t<label#label#…> (labels should be lowercase)')
    p.add_argument('--concept-uri-regex', help='Regex to filter concept IRIs (e.g., "/[0-9]+?$")')
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', default=5000, type=int)
    p.add_argument('--debug', action='store_true')
    p.add_argument('--word-freq', help='Path to word-frequency file (optional)')
    p.add_argument('--morph', help='Path to morphology-substitution file (optional)')

    return p.parse_args()

def apply_args(args):
    global endpoint_url, username, password, lang, scheme_uri, scheme_regex
    global nlp_labels_path, concept_uri_regex

    endpoint_url = args.endpoint
    username = args.username or None
    password = args.password or None
    lang = args.lang
    scheme_uri = args.scheme_uri
    scheme_regex = args.scheme_regex
    nlp_labels_path = args.nlp_labels
    concept_uri_regex = args.concept_uri_regex
    global word_freq_path, morph_bits_path
    word_freq_path = args.word_freq
    morph_bits_path = args.morph

if __name__ == '__main__':
    args = parse_args()
    apply_args(args)

    # Load thesaurus + extras
    load_thesaurus_data()
    load_nlp_labels()

    # Start API
    app.run(debug=args.debug, host=args.host, port=args.port)
