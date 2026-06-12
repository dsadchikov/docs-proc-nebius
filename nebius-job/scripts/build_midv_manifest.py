"""Build the evaluation manifest from a downloaded MIDV-2020 subset (Req 15.2).

Reads per-type template images and VIA 2.x annotation JSONs, maps MIDV field
names to our blueprint field names, and writes:

{
  "documents": [
    {
      "document_id": "esp_id_00",
      "doc_type": "esp_id",
      "blueprint_id": "id_card",
      "nos_key": "eval/midv2020/images/esp_id/00.jpg",
      "mime_type": "image/jpeg",
      "ground_truth": { "surname": "...", "given_names": "...", ... }
    }
  ]
}

VIA 2.x annotation shapes handled:
  A) Top-level key = filename (old):
       {"00.jpg": {"regions": {"0": {"region_attributes": {"label": "surname", "value": "X"}}}}}
  B) Top-level key = filename+size (VIA default):
       {"00.jpg12345": {"filename": "00.jpg", "regions": [...]}}
  C) _via_img_metadata wrapper:
       {"_via_img_metadata": { ... shape A or B inside ... }}
  D) Flat field dict (non-VIA):
       {"00": {"surname": {"value": "X"}, ...}}
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

TYPE_TO_BLUEPRINT = {
    "alb_id":             "id_card",
    "esp_id":             "id_card",
    "est_id":             "id_card",
    "fin_id":             "id_card",
    "svk_id":             "id_card",
    "aze_passport":       "passport",
    "grc_passport":       "passport",
    "lva_passport":       "passport",
    "rus_internalpassport": "passport",
    "srb_passport":       "passport",
}

FIELD_MAP = {
    "surname":          "surname",
    "name":             "given_names",
    "given_names":      "given_names",
    "birth_date":       "date_of_birth",
    "date_of_birth":    "date_of_birth",
    "expiry_date":      "date_of_expiry",
    "date_of_expiry":   "date_of_expiry",
    "issue_date":       "date_of_issue",
    "number":           "document_number",
    "document_number":  "document_number",
    "personal_number":  "personal_number",
    "id_number":        "personal_number",
    "nationality":      "nationality",
    "sex":              "sex",
    "gender":           "sex",
    "mrz_line_1":       "mrz_line_1",
    "mrz_line_2":       "mrz_line_2",
    "mrz_line_3":       "mrz_line_3",
}


def _find_via_entry(annotations: dict, filename: str) -> dict | None:
    """Return the VIA metadata entry for a given image filename."""
    # Unwrap _via_img_metadata if present
    raw = annotations.get("_via_img_metadata", annotations)

    # Try exact match first
    for key in (filename, Path(filename).stem):
        if key in raw:
            return raw[key]

    # VIA default key = filename + filesize (e.g. "00.jpg12345")
    for key, val in raw.items():
        if isinstance(val, dict) and val.get("filename") == filename:
            return val

    return None


def _parse_regions(entry: dict) -> dict:
    """Extract {blueprint_field: value} from one VIA document entry."""
    gt: dict[str, str] = {}
    regions = entry.get("regions", {})

    # Regions can be a list or a dict keyed by index
    if isinstance(regions, dict):
        regions = list(regions.values())

    for region in regions:
        attrs = region.get("region_attributes", {})
        # Keys: ("label","value") or ("field_name","value") or ("name","value")
        fname = attrs.get("label") or attrs.get("field_name") or attrs.get("name")
        value = attrs.get("value") or attrs.get("text")
        if fname and value and fname in FIELD_MAP:
            gt[FIELD_MAP[fname]] = str(value).strip()

    return gt


def extract_ground_truth(annotations: dict, filename: str) -> dict:
    """Pull {blueprint_field: value} for one document image.

    Tries VIA format first; falls back to flat field dict (shape D).
    """
    entry = _find_via_entry(annotations, filename)
    if entry is not None:
        gt = _parse_regions(entry)
        if gt:
            return gt
        # Entry found but no usable regions — try flat field dict inside entry
        return _flat_field_dict(entry)

    # Shape D: top-level key is stem, value is {field: {value: ...}} or {field: str}
    stem = Path(filename).stem
    flat = annotations.get(stem) or annotations.get(filename)
    if flat and isinstance(flat, dict):
        return _flat_field_dict(flat)

    return {}


def _flat_field_dict(d: dict) -> dict:
    gt: dict[str, str] = {}
    for key, val in d.items():
        if key not in FIELD_MAP:
            continue
        if isinstance(val, dict) and "value" in val:
            gt[FIELD_MAP[key]] = str(val["value"]).strip()
        elif isinstance(val, str):
            gt[FIELD_MAP[key]] = val.strip()
    return gt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--types", required=True)
    ap.add_argument("--per-type", type=int, default=20)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    work = Path(args.work_dir)
    upload_images = work / "upload" / "images"
    documents = []

    for doc_type in args.types.split():
        blueprint_id = TYPE_TO_BLUEPRINT.get(doc_type)
        if not blueprint_id:
            sys.exit(f"ERROR: unknown MIDV type {doc_type!r}")

        # Images: work/images/<type>/ (templates.tar structure)
        img_dir = next((p for p in [
            work / "images" / doc_type,
            work / "templates" / doc_type,
            work / doc_type / "images",
        ] if p.is_dir()), None)

        # Annotations: work/annotations/<type>.json
        ann_file = next((p for p in [
            work / "annotations" / f"{doc_type}.json",
            work / doc_type / f"{doc_type}.json",
        ] if p.is_file()), None)

        if img_dir is None or ann_file is None:
            top = sorted(p.name for p in work.iterdir())
            sys.exit(
                f"ERROR: cannot locate images/annotations for {doc_type!r}\n"
                f"  img_dir tried: {work/'images'/doc_type}, {work/'templates'/doc_type}\n"
                f"  ann_file tried: {work/'annotations'/(doc_type+'.json')}\n"
                f"  work/ top-level: {top}"
            )

        annotations = json.loads(ann_file.read_text(encoding="utf-8"))
        images = sorted(img_dir.glob("*.jpg")) + sorted(img_dir.glob("*.png"))
        if not images:
            sys.exit(f"ERROR: no images in {img_dir}")

        taken = 0
        for img in images:
            if taken >= args.per_type:
                break
            gt = extract_ground_truth(annotations, img.name)
            if not gt:
                print(f"WARN: empty ground truth for {doc_type}/{img.name} "
                      f"(ann keys sample: {list(annotations)[:3]})", file=sys.stderr)
                continue
            dest = upload_images / doc_type / img.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(img, dest)
            documents.append({
                "document_id": f"{doc_type}_{img.stem}",
                "doc_type": doc_type,
                "blueprint_id": blueprint_id,
                "nos_key": f"eval/midv2020/images/{doc_type}/{img.name}",
                "mime_type": "image/jpeg" if img.suffix == ".jpg" else "image/png",
                "ground_truth": gt,
            })
            taken += 1

        print(f">> {doc_type}: {taken} documents (blueprint={blueprint_id})")
        if taken == 0:
            print(f"   annotation sample: {json.dumps(dict(list(annotations.items())[:2]), ensure_ascii=False)[:300]}",
                  file=sys.stderr)

    if len(documents) < 50:
        print(f"WARN: only {len(documents)} documents (<50) — increase --per-type or add types",
              file=sys.stderr)

    Path(args.out).write_text(
        json.dumps({"documents": documents}, ensure_ascii=False, indent=2)
    )
    print(f">> manifest: {args.out} ({len(documents)} documents)")


if __name__ == "__main__":
    main()
