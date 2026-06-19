"""Detect diagram-heavy PDF pages and caption them once via Gemini vision.

Captions are stored in data/rules/core_rules/page_captions.json keyed to the
source PDF SHA256 from manifest.json. Run on core rules first; scale to other
profiles after reviewing actual token costs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fitz
from google import genai
from google.auth.exceptions import DefaultCredentialsError
from google.genai import types
from google.oauth2.credentials import Credentials

from roboto_guilliman.config import Settings, get_settings
from roboto_guilliman.ingestion.source_registry import DEFAULT_RULES_DIR, ParserProfile

logger = logging.getLogger(__name__)

CAPTIONS_FILENAME = "page_captions.json"
DEFAULT_CAPTION_MODEL = "gemini-2.5-pro"
DEFAULT_BUDGET_USD = 5.0
DEFAULT_RENDER_DPI = 150

CAPTION_PROMPT = """You are indexing Warhammer 40,000 11th edition (#New40k) core rules for retrieval.

This PDF page was flagged as diagram-heavy. Describe every rules diagram, flowchart,
example battlefield layout, datasheet callout, and illustrated walkthrough visible.
Write for a player looking up a rule - name phases, steps, keywords, distances, and
unit labels shown. Use plain prose (no markdown). Mention colours only when they
distinguish sides or outcomes. Write complete sentences up to 250 words."""

# Vertex list prices (USD per 1M tokens), standard tier, <=200k context.
MODEL_PRICING_USD: dict[str, tuple[float, float]] = {
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}


@dataclass(frozen=True)
class PageLayout:
    page_number: int
    drawing_count: int
    image_count: int
    text_chars: int

    @property
    def diagram_score(self) -> float:
        ratio = self.drawing_count / max(self.text_chars, 1) * 1000
        return self.drawing_count + self.image_count * 3 + ratio


@dataclass
class PageCaption:
    page_number: int
    caption: str
    prompt_tokens: int
    output_tokens: int
    cost_usd: float
    diagram_score: float
    drawing_count: int
    image_count: int
    text_chars: int
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class CaptionRunSummary:
    source_pdf: str
    sha256: str
    model: str
    generated_at: str
    render_dpi: int
    budget_usd: float
    pages_scanned: int
    pages_flagged: int
    pages_captioned: int
    pages_skipped: int
    total_prompt_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    pages: dict[str, dict[str, Any]]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest_sha256(pdf_path: Path, *, rules_dir: Path = DEFAULT_RULES_DIR) -> str | None:
    manifest_path = rules_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        relative = pdf_path.resolve().relative_to(rules_dir.resolve()).as_posix()
    except ValueError:
        return None
    for item in json.loads(manifest_path.read_text(encoding="utf-8")):
        if item.get("relative_path") == relative:
            return item.get("sha256")
    return None


def _gcloud_access_token() -> str:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        default = Path.home() / "AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"
        if default.exists():
            gcloud = str(default)
    if gcloud is None:
        raise DefaultCredentialsError(
            "gcloud not found; run `gcloud auth application-default login` or install the Cloud SDK."
        )
    return subprocess.check_output([gcloud, "auth", "print-access-token"], text=True).strip()


def build_vertex_client(settings: Settings) -> genai.Client:
    """Vertex client using ADC when present, else the active gcloud user token."""
    adc_paths = [
        Path(os.environ.get("APPDATA", "")) / "gcloud/application_default_credentials.json",
        Path.home() / ".config/gcloud/application_default_credentials.json",
    ]
    has_adc = any(path.exists() for path in adc_paths) or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    if has_adc:
        return genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
        )
    logger.info("ADC not found; using gcloud user access token")
    token = _gcloud_access_token()
    return genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location=settings.gcp_location,
        credentials=Credentials(token=token),
    )


def analyse_page_layout(page: fitz.Page, *, page_number: int) -> PageLayout:
    return PageLayout(
        page_number=page_number,
        drawing_count=len(page.get_drawings()),
        image_count=len(page.get_images(full=True)),
        text_chars=len(page.get_text("text").strip()),
    )


def is_diagram_heavy(layout: PageLayout) -> bool:
    """Heuristic tuned on the #New40k core rules PDF (~23 pages flagged)."""
    drawing_count = layout.drawing_count
    image_count = layout.image_count
    text_chars = layout.text_chars
    ratio = drawing_count / max(text_chars, 1) * 1000

    if drawing_count >= 70:
        return True
    if image_count >= 8:
        return True
    if drawing_count >= 25 and text_chars < 2000 and ratio >= 15:
        return True
    if drawing_count >= 35 and text_chars < 1200:
        return True
    if drawing_count >= 20 and text_chars < 1000 and ratio >= 20:
        return True
    if drawing_count >= 40 and text_chars < 2800 and ratio >= 12:
        return True
    return False


def flag_diagram_pages(doc: fitz.Document) -> list[PageLayout]:
    flagged: list[PageLayout] = []
    for page_number, page in enumerate(doc, start=1):
        layout = analyse_page_layout(page, page_number=page_number)
        if is_diagram_heavy(layout):
            flagged.append(layout)
    flagged.sort(key=lambda item: item.page_number)
    return flagged


def render_page_png(page: fitz.Page, *, dpi: int = DEFAULT_RENDER_DPI) -> bytes:
    scale = dpi / 72
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    return pixmap.tobytes("png")


def estimate_cost_usd(*, model: str, prompt_tokens: int, output_tokens: int) -> float:
    input_rate, output_rate = MODEL_PRICING_USD.get(model, MODEL_PRICING_USD[DEFAULT_CAPTION_MODEL])
    return (prompt_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def extract_usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0, 0
    prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    thought_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
    billable_output = output_tokens + thought_tokens
    if billable_output == 0:
        total = int(getattr(usage, "total_token_count", 0) or 0)
        billable_output = max(total - prompt_tokens, 0)
    return prompt_tokens, billable_output


def caption_page_image(
    client: genai.Client,
    *,
    model: str,
    png_bytes: bytes,
    page_number: int,
) -> tuple[str, int, int]:
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                    types.Part.from_text(
                        text=f"{CAPTION_PROMPT}\n\nPDF page number: {page_number}"
                    ),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=8192,
        ),
    )
    text = (response.text or "").strip()
    prompt_tokens, output_tokens = extract_usage(response)
    return text, prompt_tokens, output_tokens


def captions_output_path(pdf_path: Path, *, rules_dir: Path = DEFAULT_RULES_DIR) -> Path:
    try:
        relative = pdf_path.resolve().relative_to(rules_dir.resolve())
        return rules_dir / relative.parent / CAPTIONS_FILENAME
    except ValueError:
        return pdf_path.parent / CAPTIONS_FILENAME


def default_core_rules_pdf(*, rules_dir: Path = DEFAULT_RULES_DIR) -> Path:
    folder = rules_dir / ParserProfile.CORE_RULES
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"No PDFs in {folder}. Run download-rules first.")
    for path in pdfs:
        if "new40k" in path.name.lower():
            return path
    if len(pdfs) == 1:
        return pdfs[0]
    names = "\n".join(f"  {path.name}" for path in pdfs)
    raise SystemExit(
        "Multiple core rules PDFs found and none match #New40k numbering. "
        f"Pass pdf_path explicitly:\n{names}"
    )


def load_page_captions(
    pdf_path: Path,
    *,
    captions_path: Path | None = None,
    rules_dir: Path = DEFAULT_RULES_DIR,
) -> tuple[str | None, dict[int, str]]:
    """Load committed page captions keyed by 1-based PDF page number."""
    path = captions_path or captions_output_path(pdf_path, rules_dir=rules_dir)
    if not path.exists():
        return None, {}
    data = json.loads(path.read_text(encoding="utf-8"))
    captions: dict[int, str] = {}
    for page_key, entry in data.get("pages", {}).items():
        caption = str(entry.get("caption", "")).strip()
        if caption:
            captions[int(page_key)] = caption
    return data.get("sha256"), captions


def run_caption_pass(
    pdf_path: Path,
    *,
    settings: Settings | None = None,
    model: str = DEFAULT_CAPTION_MODEL,
    budget_usd: float = DEFAULT_BUDGET_USD,
    render_dpi: int = DEFAULT_RENDER_DPI,
    dry_run: bool = False,
    save_images: bool = False,
    images_dir: Path | None = None,
    rules_dir: Path = DEFAULT_RULES_DIR,
) -> CaptionRunSummary:
    if model not in MODEL_PRICING_USD:
        known = ", ".join(sorted(MODEL_PRICING_USD))
        raise ValueError(f"Unknown model {model!r}. Known models: {known}")

    settings = settings or get_settings()
    sha256 = load_manifest_sha256(pdf_path, rules_dir=rules_dir) or sha256_file(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        pages_scanned = len(doc)
        flagged = flag_diagram_pages(doc)
        pages_out: dict[str, dict[str, Any]] = {}
        total_prompt = 0
        total_output = 0
        total_cost = 0.0
        captioned = 0
        skipped = 0

        client: genai.Client | None = None
        if not dry_run:
            client = build_vertex_client(settings)

        for layout in flagged:
            page_key = str(layout.page_number)
            if total_cost >= budget_usd:
                pages_out[page_key] = {
                    "skipped": True,
                    "skip_reason": "budget_exceeded",
                    "diagram_score": round(layout.diagram_score, 2),
                    "drawing_count": layout.drawing_count,
                    "image_count": layout.image_count,
                    "text_chars": layout.text_chars,
                }
                skipped += 1
                logger.warning(
                    "Budget $%.4f reached; skipping page %s",
                    budget_usd,
                    layout.page_number,
                )
                continue

            if dry_run:
                pages_out[page_key] = {
                    "dry_run": True,
                    "diagram_score": round(layout.diagram_score, 2),
                    "drawing_count": layout.drawing_count,
                    "image_count": layout.image_count,
                    "text_chars": layout.text_chars,
                }
                continue

            assert client is not None
            page = doc[layout.page_number - 1]
            png_bytes = render_page_png(page, dpi=render_dpi)

            if save_images:
                target_dir = images_dir or (pdf_path.parent / "page_images")
                target_dir.mkdir(parents=True, exist_ok=True)
                image_path = target_dir / f"page_{layout.page_number:03d}.png"
                image_path.write_bytes(png_bytes)

            caption_text, prompt_tokens, output_tokens = caption_page_image(
                client,
                model=model,
                png_bytes=png_bytes,
                page_number=layout.page_number,
            )
            page_cost = estimate_cost_usd(
                model=model,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
            )
            total_prompt += prompt_tokens
            total_output += output_tokens
            total_cost += page_cost

            if not caption_text.strip():
                skipped += 1
                pages_out[page_key] = {
                    "caption": "",
                    "has_figure": False,
                    "skipped": True,
                    "skip_reason": "empty_response",
                    "prompt_tokens": prompt_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": round(page_cost, 6),
                    "diagram_score": round(layout.diagram_score, 2),
                    "drawing_count": layout.drawing_count,
                    "image_count": layout.image_count,
                    "text_chars": layout.text_chars,
                }
                logger.warning(
                    "Page %s: empty caption ($%.4f)",
                    layout.page_number,
                    page_cost,
                )
                continue

            captioned += 1
            pages_out[page_key] = {
                "caption": caption_text,
                "has_figure": True,
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(page_cost, 6),
                "diagram_score": round(layout.diagram_score, 2),
                "drawing_count": layout.drawing_count,
                "image_count": layout.image_count,
                "text_chars": layout.text_chars,
            }
            logger.info(
                "Page %s: %s in / %s out tokens, $%.4f, %s chars",
                layout.page_number,
                prompt_tokens,
                output_tokens,
                page_cost,
                len(caption_text),
            )
    finally:
        doc.close()

    return CaptionRunSummary(
        source_pdf=pdf_path.name,
        sha256=sha256,
        model=model,
        generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        render_dpi=render_dpi,
        budget_usd=budget_usd,
        pages_scanned=pages_scanned,
        pages_flagged=len(flagged),
        pages_captioned=captioned,
        pages_skipped=skipped,
        total_prompt_tokens=total_prompt,
        total_output_tokens=total_output,
        total_cost_usd=round(total_cost, 6),
        pages=pages_out,
    )


def save_caption_summary(summary: CaptionRunSummary, output_path: Path) -> None:
    payload = asdict(summary)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def print_cost_report(summary: CaptionRunSummary, *, output_path: Path | None = None) -> None:
    input_rate, output_rate = MODEL_PRICING_USD[summary.model]
    print(f"PDF: {summary.source_pdf}")
    print(f"SHA256: {summary.sha256}")
    print(f"Model: {summary.model} (${input_rate}/M in, ${output_rate}/M out)")
    print(f"Pages scanned: {summary.pages_scanned}")
    print(f"Pages flagged: {summary.pages_flagged}")
    if summary.pages_captioned == 0 and summary.total_cost_usd == 0:
        print("Mode: dry-run (no API calls)")
    else:
        print(f"Pages captioned: {summary.pages_captioned}")
        print(f"Pages skipped: {summary.pages_skipped}")
        print(
            f"Tokens: {summary.total_prompt_tokens:,} in + "
            f"{summary.total_output_tokens:,} out"
        )
        print(f"Actual cost: ${summary.total_cost_usd:.4f} USD")
        print(f"Budget cap: ${summary.budget_usd:.2f} USD")
        remaining = summary.budget_usd - summary.total_cost_usd
        print(f"Budget remaining: ${remaining:.4f} USD")
    if output_path is not None:
        print(f"Output: {output_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Caption diagram-heavy pages in the #New40k core rules PDF.",
    )
    parser.add_argument(
        "pdf_path",
        type=Path,
        nargs="?",
        help="Path to core rules PDF (default: data/rules/core_rules/#New40k file).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_CAPTION_MODEL,
        choices=sorted(MODEL_PRICING_USD),
        help=f"Vision model (default: {DEFAULT_CAPTION_MODEL}).",
    )
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=DEFAULT_BUDGET_USD,
        help=f"Stop after this spend (default: {DEFAULT_BUDGET_USD}).",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_RENDER_DPI,
        help=f"Page render DPI (default: {DEFAULT_RENDER_DPI}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List flagged pages only; no Vertex API calls.",
    )
    parser.add_argument(
        "--save-images",
        action="store_true",
        help="Cache rendered PNGs under data/rules/core_rules/page_images/ (gitignored).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Override output JSON path (default: page_captions.json beside PDF).",
    )
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    args = build_parser().parse_args()
    pdf_path = args.pdf_path or default_core_rules_pdf()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    summary = run_caption_pass(
        pdf_path,
        model=args.model,
        budget_usd=args.budget_usd,
        render_dpi=args.dpi,
        dry_run=args.dry_run,
        save_images=args.save_images,
    )

    output_path = args.output or captions_output_path(pdf_path)
    if not args.dry_run:
        save_caption_summary(summary, output_path)
        logger.info("Wrote %s", output_path)

    print_cost_report(summary, output_path=output_path)


if __name__ == "__main__":
    main()
