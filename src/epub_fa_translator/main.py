from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import posixpath
import re
import tempfile
import textwrap
import time
import zipfile
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from lxml import etree
from dotenv import load_dotenv
from openai import OpenAI
from pypdf import PdfReader

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_WORK_DIR = Path(".translator-work")
DEFAULT_MAX_OUTPUT_TOKENS = 32000
DEFAULT_RETRIES = 3
DEFAULT_ENV_FILE = Path(".env")
DEFAULT_ANCHOR_SCAN_CHAPTERS = 6
DEFAULT_ANCHOR_MAX_TERMS = 80
DEFAULT_ANCHOR_REVIEW_INTERVAL = 4
DEFAULT_CORPUS_CHARS_PER_CHAPTER = 12000
DEFAULT_CORPUS_TOTAL_CHARS = 80000
DEFAULT_PDF_SECTION_CHAR_TARGET = 12000
DEFAULT_PDF_MIN_SECTION_PARAGRAPHS = 6
DEFAULT_PDF_MAX_SECTION_PARAGRAPHS = 24
DEFAULT_BOOK_CONTEXT_SAMPLE_CHAPTERS = 5
DEFAULT_BOOK_CONTEXT_TOTAL_CHARS = 24000
DEFAULT_BOOK_CONTEXT_MAX_OUTPUT_TOKENS = 2200
ANCHOR_STATE_FILENAME = "concept-anchors.json"
ANCHOR_MARKDOWN_FILENAME = "concept-anchors.md"
BOOK_CONTEXT_MARKDOWN_FILENAME = "book-context.md"
XML_NS = "http://www.w3.org/XML/1998/namespace"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
CONTAINER_NS = {"container": "urn:oasis:names:tc:opendocument:xmlns:container"}
OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
XHTML_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}
CSS_RELATIVE_PATH = "styles/persian-rtl.css"
FONT_RELATIVE_PATH = "fonts/Vazirmatn-Regular.ttf"
FONT_DOWNLOAD_CANDIDATES = (
    "https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/fonts/ttf/Vazirmatn-Regular.ttf",
    "https://cdn.jsdelivr.net/npm/vazirmatn@33.003/fonts/ttf/Vazirmatn-Regular.ttf",
)
PSYCHOLOGICAL_TRANSLATION_BRIEF = textwrap.dedent(
    """
    This project is especially intended for deep psychological, psychoanalytic, introspective,
    and concept-heavy books.
    - preserve conceptual precision and emotional nuance
    - do not flatten ambiguity, tension, or layered inner states into simplistic prose
    - keep specialized psychological or philosophical terminology consistent across chapters
    - when a term has a well-established Persian equivalent, prefer it over ad hoc literal phrasing
    - if a sentence is dense, make it natural in Persian without diluting its intellectual weight
    - maintain the author's reflective tone, analytic cadence, and subtle shifts in register
    """
).strip()
CHAPTER_HEADING_RE = re.compile(
    r"^(chapter|part)\s+(?:\d+|[ivxlcdm]+)\b(?:\s*[:.\-]\s*.+)?$",
    re.IGNORECASE,
)
SPECIAL_HEADING_RE = re.compile(
    r"^(prologue|epilogue|introduction|foreword|preface|afterword|conclusion|appendix(?:\s+[a-z0-9]+)?)$",
    re.IGNORECASE,
)

@dataclass(frozen=True)
class ChapterTarget:
    index: int
    label: str
    relative_path: str
    absolute_path: Path


@dataclass(frozen=True)
class PdfSection:
    title: str
    paragraphs: tuple[str, ...]


@dataclass(frozen=True)
class BookMetadata:
    title: str
    author: str = ""
    description: str = ""
    subjects: tuple[str, ...] = ()
    publisher: str = ""
    language: str = ""
    identifier: str = ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_environment(args.env_file)
    args.model = resolve_model_name(args.model)
    input_epub = args.input_epub.resolve()
    output_epub = resolve_output_path(input_epub, args.output_epub)
    work_dir = args.work_dir.resolve()
    book_work_dir = work_dir / safe_slug(input_epub.stem)
    anchor_state_path = book_work_dir / ANCHOR_STATE_FILENAME
    anchor_markdown_path = book_work_dir / ANCHOR_MARKDOWN_FILENAME
    translation_context = load_translation_context(args.context, args.context_file)

    provider_name = resolve_api_provider_name()
    client = create_openai_client()
    print(f"Using {provider_name} with model {args.model}.")

    with tempfile.TemporaryDirectory(prefix="epub-fa-") as temp_dir:
        extracted_dir = Path(temp_dir) / "book"
        prepare_source_book(input_epub, extracted_dir)

        opf_path = find_opf_path(extracted_dir)
        opf_tree = parse_xml_file(opf_path)
        book_root = opf_path.parent
        book_metadata = extract_book_metadata(opf_tree, fallback_title=input_epub.stem)

        font_bytes = resolve_vazirmatn_bytes(args.font_path, args.font_url)
        install_assets(book_root, font_bytes)
        ensure_manifest_assets(opf_tree)
        update_package_language(opf_tree)

        chapters = collect_translation_targets(opf_tree, opf_path)
        if not chapters:
            raise RuntimeError("No XHTML chapters were found in the EPUB spine.")

        book_work_dir.mkdir(parents=True, exist_ok=True)
        book_context = initialize_book_context(
            client=client,
            model=args.model,
            metadata=book_metadata,
            chapters=chapters,
            context_path=book_work_dir / BOOK_CONTEXT_MARKDOWN_FILENAME,
            max_output_tokens=args.max_output_tokens,
            retries=args.retries,
            force=args.force,
        )
        print(f"Found {len(chapters)} chapter files to translate.")

        anchor_state = initialize_concept_anchors(
            client=client,
            model=args.model,
            chapters=chapters,
            translation_context=translation_context,
            work_dir=book_work_dir,
            state_path=anchor_state_path,
            markdown_path=anchor_markdown_path,
            max_output_tokens=args.max_output_tokens,
            retries=args.retries,
            force=args.force,
            skip=args.skip_concept_anchoring,
            anchor_scan_chapters=args.anchor_scan_chapters,
            anchor_max_terms=args.anchor_max_terms,
        )

        for chapter in chapters:
            anchor_state = process_chapter(
                client=client,
                chapter=chapter,
                total_chapters=len(chapters),
                model=args.model,
                book_context=book_context,
                translation_context=translation_context,
                work_dir=book_work_dir,
                force=args.force,
                max_output_tokens=args.max_output_tokens,
                retries=args.retries,
                anchor_state=anchor_state,
                anchor_state_path=anchor_state_path,
                anchor_markdown_path=anchor_markdown_path,
                skip_concept_anchoring=args.skip_concept_anchoring,
                anchor_review_interval=args.anchor_review_interval,
            )

        write_xml_file(opf_path, opf_tree)
        build_epub(extracted_dir, output_epub)

    print(f"Written Persian EPUB: {output_epub}")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate an EPUB or PDF book into a polished Persian EPUB chapter by chapter."
    )
    parser.add_argument("input_epub", type=Path, help="Source EPUB or PDF file")
    parser.add_argument(
        "output_epub",
        nargs="?",
        type=Path,
        help="Output EPUB path. Defaults to <input>.fa.epub",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Model/deployment name to use. Defaults to MODEL env var if set, otherwise {DEFAULT_MODEL}.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Path to the .env file. Default: {DEFAULT_ENV_FILE}",
    )
    parser.add_argument(
        "--context",
        default="",
        help="Extra translation guidance appended to the model instructions.",
    )
    parser.add_argument(
        "--context-file",
        type=Path,
        help="Path to a text file with extra translation guidance.",
    )
    parser.add_argument(
        "--skip-concept-anchoring",
        action="store_true",
        help="Disable the glossary-based Progressive Concept Anchoring pipeline.",
    )
    parser.add_argument(
        "--anchor-scan-chapters",
        type=int,
        default=DEFAULT_ANCHOR_SCAN_CHAPTERS,
        help=(
            "How many early chapters to scan when building the initial concept anchors. "
            "Use 0 to scan all chapters."
        ),
    )
    parser.add_argument(
        "--anchor-max-terms",
        type=int,
        default=DEFAULT_ANCHOR_MAX_TERMS,
        help=f"Maximum concept anchors to keep. Default: {DEFAULT_ANCHOR_MAX_TERMS}",
    )
    parser.add_argument(
        "--anchor-review-interval",
        type=int,
        default=DEFAULT_ANCHOR_REVIEW_INTERVAL,
        help=(
            "After how many chapters the glossary is drift-reviewed against recent translations. "
            f"Use 0 to disable. Default: {DEFAULT_ANCHOR_REVIEW_INTERVAL}"
        ),
    )
    parser.add_argument(
        "--font-path",
        type=Path,
        help="Optional local path to Vazirmatn-Regular.ttf.",
    )
    parser.add_argument(
        "--font-url",
        help="Optional direct URL for a Vazirmatn TTF file.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK_DIR,
        help=f"Directory for per-chapter checkpoints. Default: {DEFAULT_WORK_DIR}",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        help=f"Max output tokens per translated chapter. Default: {DEFAULT_MAX_OUTPUT_TOKENS}",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Retries per chapter if output is invalid. Default: {DEFAULT_RETRIES}",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-translate chapters even if cached checkpoints already exist.",
    )
    return parser.parse_args(argv)


def resolve_output_path(input_epub: Path, output_epub: Path | None) -> Path:
    if output_epub is not None:
        return output_epub.resolve()
    return input_epub.with_name(f"{input_epub.stem}.fa.epub").resolve()


def resolve_api_provider_name() -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return "OpenAI"
    if os.environ.get("AZURE_API_ENDPOINT") and os.environ.get("AZURE_API_KEY"):
        return "Azure OpenAI"
    if os.environ.get("AZURE_API_ENDPOINT") or os.environ.get("AZURE_API_KEY"):
        raise RuntimeError(
            "Azure OpenAI configuration is incomplete. Set both AZURE_API_ENDPOINT and "
            "AZURE_API_KEY, or set OPENAI_API_KEY."
        )
    raise RuntimeError(
        "Set OPENAI_API_KEY in your environment or .env file, or set both "
        "AZURE_API_ENDPOINT and AZURE_API_KEY for Azure OpenAI."
    )


def resolve_model_name(cli_model: str | None) -> str:
    if cli_model:
        return cli_model
    env_model = os.environ.get("MODEL", "").strip()
    if env_model:
        return env_model
    return DEFAULT_MODEL


def create_openai_client() -> OpenAI:
    provider_name = resolve_api_provider_name()
    if provider_name == "Azure OpenAI":
        endpoint = os.environ["AZURE_API_ENDPOINT"].rstrip("/")
        return OpenAI(
            base_url=f"{endpoint}/openai/v1",
            api_key=os.environ["AZURE_API_KEY"],
            default_headers={"x-ms-useragent": "AzureOpenAI.Studio/ai.azure.com"},
            max_retries=3,
            timeout=600.0,
        )

    return OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=3, timeout=600.0)


def load_environment(env_file: Path) -> None:
    load_dotenv(dotenv_path=env_file, override=False)


def load_translation_context(inline_context: str, context_file: Path | None) -> str:
    parts: list[str] = []
    if inline_context.strip():
        parts.append(inline_context.strip())
    if context_file:
        parts.append(context_file.read_text(encoding="utf-8").strip())
    return "\n\n".join(part for part in parts if part)


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_opf_text_values(opf_tree: etree._ElementTree, xpath: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in opf_tree.xpath(xpath, namespaces=OPF_NS):
        if isinstance(match, etree._Element):
            text = " ".join(part.strip() for part in match.itertext() if part and part.strip())
        else:
            text = str(match)
        normalized = normalize_whitespace(text)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        values.append(normalized)
    return values


def extract_book_metadata(opf_tree: etree._ElementTree, fallback_title: str) -> BookMetadata:
    title_values = extract_opf_text_values(opf_tree, "/opf:package/opf:metadata/dc:title")
    creator_values = extract_opf_text_values(opf_tree, "/opf:package/opf:metadata/dc:creator")
    description_values = [
        *extract_opf_text_values(opf_tree, "/opf:package/opf:metadata/dc:description"),
        *extract_opf_text_values(
            opf_tree,
            "/opf:package/opf:metadata/opf:meta[translate(@property, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz')='description']",
        ),
        *extract_opf_text_values(
            opf_tree,
            "/opf:package/opf:metadata/opf:meta[translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz')='description']/@content",
        ),
    ]
    subject_values = extract_opf_text_values(opf_tree, "/opf:package/opf:metadata/dc:subject")
    publisher_values = extract_opf_text_values(opf_tree, "/opf:package/opf:metadata/dc:publisher")
    language_values = extract_opf_text_values(opf_tree, "/opf:package/opf:metadata/dc:language")
    identifier_values = extract_opf_text_values(opf_tree, "/opf:package/opf:metadata/dc:identifier")

    return BookMetadata(
        title=title_values[0] if title_values else humanize_title(fallback_title),
        author=", ".join(creator_values),
        description=description_values[0] if description_values else "",
        subjects=tuple(subject_values),
        publisher=publisher_values[0] if publisher_values else "",
        language=language_values[0] if language_values else "",
        identifier=identifier_values[0] if identifier_values else "",
    )


def initialize_book_context(
    client: OpenAI,
    model: str,
    metadata: BookMetadata,
    chapters: list[ChapterTarget],
    context_path: Path,
    max_output_tokens: int,
    retries: int,
    force: bool,
) -> str:
    if context_path.exists() and not force:
        cached = context_path.read_text(encoding="utf-8").strip()
        if cached:
            print("Loaded cached book context dossier.")
            return cached

    fallback_context = build_fallback_book_context(metadata, len(chapters))
    corpus = build_book_context_corpus(chapters)
    if not corpus.strip():
        context_path.write_text(f"{fallback_context}\n", encoding="utf-8")
        return fallback_context

    print("Building book context dossier...")
    try:
        payload = request_json_object(
            client=client,
            model=model,
            instructions=build_book_context_instructions(),
            prompt=build_book_context_prompt(metadata=metadata, chapter_count=len(chapters), corpus=corpus),
            max_output_tokens=min(max_output_tokens, DEFAULT_BOOK_CONTEXT_MAX_OUTPUT_TOKENS),
            retries=retries,
            purpose="build book context dossier",
        )
        book_context = format_book_context_markdown(metadata, len(chapters), payload)
    except Exception as exc:  # noqa: BLE001
        print(f"  Warning: could not synthesize book context ({exc}). Using extracted metadata only.")
        book_context = fallback_context

    context_path.write_text(f"{book_context.rstrip()}\n", encoding="utf-8")
    return book_context


def build_book_context_instructions() -> str:
    return textwrap.dedent(
        """
        You are preparing a compact translator dossier for an English-to-Persian book translation.

        Infer only what is well supported by the metadata and source excerpts.
        - identify the book's likely subject matter, scope, tone, and stylistic posture
        - summarize the book in concise prose useful to a translator
        - highlight recurring themes or conceptual domains that should remain coherent
        - call out translation priorities such as terminology control, register, or voice
        - do not invent unsupported plot details, names, or claims
        - if evidence is incomplete, keep the wording cautious
        - return valid JSON only

        JSON schema:
        {
          "summary": "2-4 sentence translator-facing summary",
          "genre_and_scope": "one short paragraph",
          "tone_and_style": ["note", "..."],
          "core_topics": ["topic", "..."],
          "translation_priorities": ["priority", "..."]
        }
        """
    ).strip()


def build_book_context_prompt(metadata: BookMetadata, chapter_count: int, corpus: str) -> str:
    metadata_lines = [
        f"Title: {metadata.title or 'Unknown'}",
        f"Author: {metadata.author or 'Unknown'}",
        f"Language: {metadata.language or 'Unknown'}",
        f"Publisher: {metadata.publisher or 'Unknown'}",
        f"Identifier: {metadata.identifier or 'Unknown'}",
        f"Chapter count: {chapter_count}",
    ]
    if metadata.subjects:
        metadata_lines.append(f"Subjects: {', '.join(metadata.subjects)}")
    if metadata.description:
        metadata_lines.append(f"Description: {metadata.description}")

    return textwrap.dedent(
        f"""
        Build a book-context dossier for downstream chapter-by-chapter translation.

        Book metadata:
        {chr(10).join(metadata_lines)}

        Representative source excerpts:
        {corpus}
        """
    ).strip()


def format_book_context_markdown(metadata: BookMetadata, chapter_count: int, payload: dict) -> str:
    summary = normalize_whitespace(str(payload.get("summary", "")))
    genre_and_scope = normalize_whitespace(str(payload.get("genre_and_scope", "")))
    tone_and_style = normalize_string_list(payload.get("tone_and_style", []))
    core_topics = normalize_string_list(payload.get("core_topics", []))
    translation_priorities = normalize_string_list(payload.get("translation_priorities", []))

    lines = [
        "# Book Context",
        "",
        "## Metadata",
        f"- Title: {metadata.title or 'Unknown'}",
        f"- Author: {metadata.author or 'Unknown'}",
        f"- Source language: {metadata.language or 'Unknown'}",
        f"- Chapter count: {chapter_count}",
    ]
    if metadata.publisher:
        lines.append(f"- Publisher: {metadata.publisher}")
    if metadata.subjects:
        lines.append(f"- Subjects: {', '.join(metadata.subjects)}")
    if metadata.description:
        lines.extend(["", "## Source Description", metadata.description])
    if summary:
        lines.extend(["", "## Summary", summary])
    if genre_and_scope:
        lines.extend(["", "## Genre And Scope", genre_and_scope])
    if tone_and_style:
        lines.extend(["", "## Tone And Style", *[f"- {item}" for item in tone_and_style]])
    if core_topics:
        lines.extend(["", "## Core Topics", *[f"- {item}" for item in core_topics]])
    if translation_priorities:
        lines.extend(
            ["", "## Translation Priorities", *[f"- {item}" for item in translation_priorities]]
        )
    return "\n".join(lines)


def build_fallback_book_context(metadata: BookMetadata, chapter_count: int) -> str:
    lines = [
        "# Book Context",
        "",
        "## Metadata",
        f"- Title: {metadata.title or 'Unknown'}",
        f"- Author: {metadata.author or 'Unknown'}",
        f"- Source language: {metadata.language or 'Unknown'}",
        f"- Chapter count: {chapter_count}",
    ]
    if metadata.publisher:
        lines.append(f"- Publisher: {metadata.publisher}")
    if metadata.subjects:
        lines.append(f"- Subjects: {', '.join(metadata.subjects)}")
    if metadata.description:
        lines.extend(["", "## Source Description", metadata.description])
    return "\n".join(lines)


def select_book_context_chapters(chapters: list[ChapterTarget]) -> list[ChapterTarget]:
    if len(chapters) <= DEFAULT_BOOK_CONTEXT_SAMPLE_CHAPTERS:
        return chapters

    sample_indices: set[int] = set()
    last_index = len(chapters) - 1
    for slot in range(DEFAULT_BOOK_CONTEXT_SAMPLE_CHAPTERS):
        sample_indices.add(round(slot * last_index / max(1, DEFAULT_BOOK_CONTEXT_SAMPLE_CHAPTERS - 1)))

    selected = [chapters[index] for index in sorted(sample_indices)]
    if len(selected) >= DEFAULT_BOOK_CONTEXT_SAMPLE_CHAPTERS:
        return selected[:DEFAULT_BOOK_CONTEXT_SAMPLE_CHAPTERS]

    selected_lookup = {chapter.index for chapter in selected}
    for chapter in chapters:
        if chapter.index in selected_lookup:
            continue
        selected.append(chapter)
        if len(selected) >= DEFAULT_BOOK_CONTEXT_SAMPLE_CHAPTERS:
            break
    return sorted(selected, key=lambda chapter: chapter.index)


def build_book_context_corpus(chapters: list[ChapterTarget]) -> str:
    parts: list[str] = []
    selected = select_book_context_chapters(chapters)
    if not selected:
        return ""

    per_chapter_char_limit = max(1500, DEFAULT_BOOK_CONTEXT_TOTAL_CHARS // len(selected))
    total_chars = 0
    for chapter in selected:
        chapter_text = extract_visible_text_from_xhtml(read_xmlish_text(chapter.absolute_path))
        if not chapter_text:
            continue
        excerpt = chapter_text[:per_chapter_char_limit]
        block = (
            f"Chapter {chapter.index}: {chapter.label}\n"
            f"Path: {chapter.relative_path}\n"
            f"Excerpt:\n{excerpt}"
        )
        projected = total_chars + len(block)
        if projected > DEFAULT_BOOK_CONTEXT_TOTAL_CHARS and parts:
            break
        parts.append(block)
        total_chars = projected
    return "\n\n".join(parts)


def prepare_source_book(input_path: Path, extracted_dir: Path) -> None:
    suffix = input_path.suffix.lower()
    if suffix == ".epub":
        unpack_epub(input_path, extracted_dir)
        return
    if suffix == ".pdf":
        build_source_epub_from_pdf(input_path, extracted_dir)
        return
    raise RuntimeError("Source file must be an .epub or .pdf file.")


def build_source_epub_from_pdf(pdf_path: Path, extracted_dir: Path) -> None:
    print("Extracting text from PDF and generating a source EPUB...")
    reader = PdfReader(str(pdf_path))
    sections = extract_pdf_sections(reader, pdf_path)
    if not sections:
        raise RuntimeError(
            "Could not extract readable text from the PDF. If this is a scanned/image PDF, "
            "run OCR first and try again."
        )

    metadata = getattr(reader, "metadata", None)
    title = clean_pdf_metadata_value(get_pdf_metadata_value(metadata, "/Title")) or humanize_title(
        pdf_path.stem
    )
    author = clean_pdf_metadata_value(get_pdf_metadata_value(metadata, "/Author"))

    write_pdf_source_epub(
        extracted_dir=extracted_dir,
        title=title,
        author=author,
        sections=sections,
        identifier=f"pdf-{safe_slug(pdf_path.stem)}",
    )


def get_pdf_metadata_value(metadata: object, key: str) -> str:
    if metadata is None:
        return ""
    try:
        value = metadata.get(key)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        value = None
    if value is None:
        attribute_name = key.lstrip("/").lower()
        value = getattr(metadata, attribute_name, "")
    return str(value or "").strip()


def clean_pdf_metadata_value(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if cleaned.lower() in {"", "untitled", "untitled document", "anonymous"}:
        return ""
    return cleaned


def humanize_title(value: str) -> str:
    text = re.sub(r"[_-]+", " ", value).strip()
    return re.sub(r"\s+", " ", text).title() or "Untitled Book"


def extract_pdf_sections(reader: PdfReader, pdf_path: Path) -> list[PdfSection]:
    page_lines: list[list[str]] = []
    repeated_short_lines: Counter[str] = Counter()

    for page in reader.pages:
        raw_text = page.extract_text() or ""
        lines = [normalize_pdf_line(line) for line in raw_text.replace("\r", "\n").split("\n")]
        page_lines.append(lines)
        repeated_short_lines.update(
            {
                line
                for line in lines
                if line
                and len(line) <= 80
                and not is_page_number_line(line)
                and not is_probable_pdf_heading(line)
            }
        )

    minimum_repetitions = max(3, max(1, len(page_lines) // 5))
    repeated_headers = {
        line
        for line, count in repeated_short_lines.items()
        if count >= minimum_repetitions
    }

    blocks: list[tuple[str, str]] = []
    for lines in page_lines:
        blocks.extend(extract_blocks_from_pdf_page(lines, repeated_headers))

    sections = assemble_pdf_sections(
        blocks=blocks,
        fallback_title=humanize_title(pdf_path.stem),
    )
    return sections


def normalize_pdf_line(line: str) -> str:
    line = line.replace("\x00", " ")
    line = line.replace("\u00ad", "")
    return re.sub(r"\s+", " ", line).strip()


def is_page_number_line(line: str) -> bool:
    return bool(
        re.fullmatch(r"(?:page\s+)?(?:\d+|[ivxlcdm]+)", line.strip(), flags=re.IGNORECASE)
    )


def is_probable_pdf_heading(line: str) -> bool:
    text = line.strip()
    if not text or is_page_number_line(text):
        return False
    if CHAPTER_HEADING_RE.fullmatch(text) or SPECIAL_HEADING_RE.fullmatch(text):
        return True
    if len(text) > 90 or any(text.endswith(mark) for mark in (".", "!", "?", ";", ",")):
        return False
    words = text.split()
    if len(words) > 10:
        return False
    if text.isupper():
        return True
    title_like_words = sum(word[:1].isupper() for word in words)
    return title_like_words >= max(2, len(words) - 1)


def extract_blocks_from_pdf_page(
    lines: list[str],
    repeated_headers: set[str],
) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    current: list[str] = []

    def flush_paragraph() -> None:
        if not current:
            return
        paragraph = " ".join(current).strip()
        if paragraph:
            blocks.append(("paragraph", paragraph))
        current.clear()

    for line in lines:
        if not line:
            flush_paragraph()
            continue
        if line in repeated_headers or is_page_number_line(line):
            flush_paragraph()
            continue
        if is_probable_pdf_heading(line):
            flush_paragraph()
            blocks.append(("heading", line))
            continue

        if current and current[-1].endswith("-") and line[:1].islower():
            current[-1] = current[-1][:-1] + line
        else:
            current.append(line)

    flush_paragraph()
    return blocks


def assemble_pdf_sections(
    blocks: list[tuple[str, str]],
    fallback_title: str,
) -> list[PdfSection]:
    sections: list[PdfSection] = []
    current_title = fallback_title
    current_paragraphs: list[str] = []

    for block_type, text in blocks:
        if block_type == "heading":
            # PDF page headers often repeat the current section title on every page.
            # Treat equivalent consecutive headings as running headers, not new sections.
            if are_equivalent_pdf_headings(text, current_title):
                continue
            if current_paragraphs:
                sections.extend(chunk_pdf_section(current_title, current_paragraphs))
                current_paragraphs = []
            current_title = text
            continue
        current_paragraphs.append(text)

    if current_paragraphs:
        sections.extend(chunk_pdf_section(current_title, current_paragraphs))

    if sections:
        return sections

    paragraph_texts = [text for block_type, text in blocks if block_type == "paragraph" and text]
    return chunk_pdf_section(fallback_title, paragraph_texts)


def chunk_pdf_section(title: str, paragraphs: list[str]) -> list[PdfSection]:
    cleaned = [paragraph.strip() for paragraph in paragraphs if paragraph.strip()]
    if not cleaned:
        return []

    chunks: list[PdfSection] = []
    current: list[str] = []
    current_chars = 0

    for paragraph in cleaned:
        should_split = (
            current
            and (
                len(current) >= DEFAULT_PDF_MAX_SECTION_PARAGRAPHS
                or (
                    current_chars + len(paragraph) > DEFAULT_PDF_SECTION_CHAR_TARGET
                    and len(current) >= DEFAULT_PDF_MIN_SECTION_PARAGRAPHS
                )
            )
        )
        if should_split:
            chunks.append(make_pdf_section(title, len(chunks) + 1, current))
            current = []
            current_chars = 0

        current.append(paragraph)
        current_chars += len(paragraph)

    if current:
        chunks.append(make_pdf_section(title, len(chunks) + 1, current))

    return chunks


def are_equivalent_pdf_headings(left: str, right: str) -> bool:
    return normalize_pdf_heading_key(left) == normalize_pdf_heading_key(right)


def normalize_pdf_heading_key(text: str) -> str:
    normalized = normalize_pdf_line(text).casefold()
    normalized = re.sub(r"^[^a-z0-9ivxlcdm]+|[^a-z0-9ivxlcdm]+$", "", normalized)
    normalized = re.sub(r"^the\s+", "", normalized)
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def make_pdf_section(title: str, index: int, paragraphs: list[str]) -> PdfSection:
    if index == 1:
        final_title = title
    else:
        final_title = f"{title} - Section {index}"
    return PdfSection(title=final_title, paragraphs=tuple(paragraphs))


def write_pdf_source_epub(
    extracted_dir: Path,
    title: str,
    author: str,
    sections: list[PdfSection],
    identifier: str,
) -> None:
    extracted_dir.mkdir(parents=True, exist_ok=True)
    (extracted_dir / "mimetype").write_text("application/epub+zip", encoding="utf-8")

    meta_inf_dir = extracted_dir / "META-INF"
    meta_inf_dir.mkdir(parents=True, exist_ok=True)
    (meta_inf_dir / "container.xml").write_text(build_container_xml(), encoding="utf-8")

    oebps_dir = extracted_dir / "OEBPS"
    chapters_dir = oebps_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    chapter_items: list[tuple[str, str, str]] = []
    for index, section in enumerate(sections, start=1):
        file_name = f"chapters/{index:03d}-{safe_slug(section.title)}.xhtml"
        chapter_path = oebps_dir / file_name
        chapter_path.write_text(build_pdf_chapter_xhtml(section), encoding="utf-8")
        chapter_items.append((f"chapter-{index}", file_name, section.title))

    (oebps_dir / "nav.xhtml").write_text(build_pdf_nav_xhtml(title, chapter_items), encoding="utf-8")
    (oebps_dir / "content.opf").write_text(
        build_pdf_opf(title=title, author=author, identifier=identifier, chapter_items=chapter_items),
        encoding="utf-8",
    )


def build_container_xml() -> str:
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">',
            "  <rootfiles>",
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>',
            "  </rootfiles>",
            "</container>",
            "",
        ]
    )


def build_pdf_chapter_xhtml(section: PdfSection) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">',
        "  <head>",
        f"    <title>{escape(section.title)}</title>",
        '    <meta charset="utf-8" />',
        "  </head>",
        "  <body>",
        "    <section>",
        f"      <h1>{escape(section.title)}</h1>",
    ]
    lines.extend(f"      <p>{escape(paragraph)}</p>" for paragraph in section.paragraphs)
    lines.extend(
        [
            "    </section>",
            "  </body>",
            "</html>",
            "",
        ]
    )
    return "\n".join(lines)


def build_pdf_nav_xhtml(title: str, chapter_items: list[tuple[str, str, str]]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">',
        "  <head>",
        f"    <title>{escape(title)}</title>",
        '    <meta charset="utf-8" />',
        "  </head>",
        "  <body>",
        '    <nav epub:type="toc" id="toc">',
        f"      <h1>{escape(title)}</h1>",
        "      <ol>",
    ]
    lines.extend(
        f'        <li><a href="{escape(href)}">{escape(label)}</a></li>'
        for _item_id, href, label in chapter_items
    )
    lines.extend(
        [
            "      </ol>",
            "    </nav>",
            "  </body>",
            "</html>",
            "",
        ]
    )
    return "\n".join(lines)


def build_pdf_opf(
    title: str,
    author: str,
    identifier: str,
    chapter_items: list[tuple[str, str, str]],
) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<package version="3.0" unique-identifier="bookid" xmlns="http://www.idpf.org/2007/opf">',
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
        f'    <dc:identifier id="bookid">{escape(identifier)}</dc:identifier>',
        f"    <dc:title>{escape(title)}</dc:title>",
        "    <dc:language>en</dc:language>",
    ]
    if author:
        lines.append(f"    <dc:creator>{escape(author)}</dc:creator>")
    lines.extend(
        [
            "  </metadata>",
            "  <manifest>",
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        ]
    )
    lines.extend(
        f'    <item id="{escape(item_id)}" href="{escape(href)}" media-type="application/xhtml+xml"/>'
        for item_id, href, _label in chapter_items
    )
    lines.extend(
        [
            "  </manifest>",
            "  <spine>",
        ]
    )
    lines.extend(
        f'    <itemref idref="{escape(item_id)}"/>'
        for item_id, _href, _label in chapter_items
    )
    lines.extend(
        [
            "  </spine>",
            "</package>",
            "",
        ]
    )
    return "\n".join(lines)


def initialize_concept_anchors(
    client: OpenAI,
    model: str,
    chapters: list[ChapterTarget],
    translation_context: str,
    work_dir: Path,
    state_path: Path,
    markdown_path: Path,
    max_output_tokens: int,
    retries: int,
    force: bool,
    skip: bool,
    anchor_scan_chapters: int,
    anchor_max_terms: int,
) -> dict:
    if skip:
        return empty_anchor_state(anchor_scan_chapters, anchor_max_terms)

    if state_path.exists() and not force:
        state = load_anchor_state(state_path, anchor_scan_chapters, anchor_max_terms)
        write_anchor_markdown(markdown_path, state)
        print(f"Loaded {len(state['entries'])} saved concept anchors.")
        return state

    scan_targets = chapters if anchor_scan_chapters <= 0 else chapters[:anchor_scan_chapters]
    corpus = build_anchor_corpus(scan_targets)
    state = empty_anchor_state(anchor_scan_chapters, anchor_max_terms)

    if not corpus.strip():
        save_anchor_state(state_path, markdown_path, state)
        return state

    print("Building initial concept anchors...")
    payload = request_json_object(
        client=client,
        model=model,
        instructions=build_anchor_extraction_instructions(
            anchor_max_terms=anchor_max_terms,
            translation_context=translation_context,
        ),
        prompt=build_anchor_extraction_prompt(
            corpus=corpus,
            chapter_count=len(scan_targets),
            anchor_max_terms=anchor_max_terms,
        ),
        max_output_tokens=max_output_tokens,
        retries=retries,
        purpose="build concept anchors",
    )
    state["entries"] = normalize_anchor_entries(payload.get("entries", []), anchor_max_terms)
    state["global_notes"] = normalize_string_list(payload.get("global_notes", []))
    state["updated_at"] = utc_now()
    save_anchor_state(state_path, markdown_path, state)
    print(f"Anchored {len(state['entries'])} concepts for translation consistency.")
    return state


def empty_anchor_state(anchor_scan_chapters: int, anchor_max_terms: int) -> dict:
    return {
        "version": 1,
        "entries": [],
        "global_notes": [],
        "translated_chapters": [],
        "last_reviewed_chapter": 0,
        "anchor_scan_chapters": max(0, anchor_scan_chapters),
        "anchor_max_terms": max(1, anchor_max_terms),
        "updated_at": utc_now(),
    }


def load_anchor_state(path: Path, anchor_scan_chapters: int, anchor_max_terms: int) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_anchor_state(anchor_scan_chapters, anchor_max_terms)

    state = empty_anchor_state(anchor_scan_chapters, anchor_max_terms)
    if isinstance(raw, dict):
        state["entries"] = normalize_anchor_entries(
            raw.get("entries", []),
            int(raw.get("anchor_max_terms", anchor_max_terms) or anchor_max_terms),
        )
        state["global_notes"] = normalize_string_list(raw.get("global_notes", []))
        state["translated_chapters"] = normalize_int_list(raw.get("translated_chapters", []))
        state["last_reviewed_chapter"] = max(
            0,
            int(raw.get("last_reviewed_chapter", 0) or 0),
        )
        state["anchor_scan_chapters"] = max(
            0,
            int(raw.get("anchor_scan_chapters", anchor_scan_chapters) or anchor_scan_chapters),
        )
        state["anchor_max_terms"] = max(
            1,
            int(raw.get("anchor_max_terms", anchor_max_terms) or anchor_max_terms),
        )
        state["updated_at"] = str(raw.get("updated_at") or utc_now())
    return state


def save_anchor_state(state_path: Path, markdown_path: Path, state: dict) -> None:
    state["updated_at"] = utc_now()
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    write_anchor_markdown(markdown_path, state)


def write_anchor_markdown(path: Path, state: dict) -> None:
    lines = [
        "# Concept Anchors",
        "",
        f"- Updated: {state.get('updated_at', '')}",
        f"- Anchors: {len(state.get('entries', []))}",
        f"- Translated chapters recorded: {len(state.get('translated_chapters', []))}",
        "",
    ]

    notes = state.get("global_notes", [])
    if notes:
        lines.append("## Global Notes")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("## Terms")
    lines.append("")
    entries = state.get("entries", [])
    if not entries:
        lines.append("- No anchors yet.")
    else:
        for entry in entries:
            source_term = entry["source_term"]
            target_term = entry["target_term"]
            note = entry.get("note", "")
            line = f"- `{source_term}` -> `{target_term}`"
            if note:
                line = f"{line}: {note}"
            lines.append(line)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def build_anchor_corpus(chapters: list[ChapterTarget]) -> str:
    parts: list[str] = []
    total_chars = 0

    for chapter in chapters:
        xhtml_text = read_xmlish_text(chapter.absolute_path)
        chapter_text = extract_visible_text_from_xhtml(xhtml_text)
        if not chapter_text:
            continue
        chapter_text = chapter_text[:DEFAULT_CORPUS_CHARS_PER_CHAPTER]
        block = (
            f"Chapter {chapter.index}: {chapter.label}\n"
            f"Path: {chapter.relative_path}\n"
            f"{chapter_text}"
        )
        projected = total_chars + len(block)
        if projected > DEFAULT_CORPUS_TOTAL_CHARS and parts:
            break
        parts.append(block)
        total_chars = projected

    return "\n\n".join(parts)


def extract_visible_text_from_xhtml(xhtml_text: str) -> str:
    try:
        parser = etree.XMLParser(remove_blank_text=False, recover=True, resolve_entities=False)
        root = etree.fromstring(xhtml_text.encode("utf-8"), parser=parser)
        texts = []
        for text in root.itertext():
            cleaned = re.sub(r"\s+", " ", text).strip()
            if cleaned:
                texts.append(cleaned)
        return re.sub(r"\s+", " ", " ".join(texts)).strip()
    except etree.XMLSyntaxError:
        stripped = re.sub(r"<[^>]+>", " ", xhtml_text)
        return re.sub(r"\s+", " ", stripped).strip()


def build_anchor_extraction_instructions(anchor_max_terms: int, translation_context: str) -> str:
    extra = (
        f"\n\nAdditional translation context:\n{translation_context.strip()}"
        if translation_context
        else ""
    )
    return textwrap.dedent(
        f"""
        You are building a Progressive Concept Anchoring glossary for an English-to-Persian
        translation of a philosophical or psychological book.

        Extract the most important recurring concepts, terms, and proper nouns that should remain
        consistent across the book.
        - focus on philosophical, psychoanalytic, introspective, spiritual, and concept-heavy terms
        - prefer established Persian terminology when it exists
        - include short notes when a term has nuance or a restricted sense
        - keep the glossary compact and high-signal
        - return valid JSON only
        - return at most {anchor_max_terms} entries

        JSON schema:
        {{
          "global_notes": ["short note", "..."],
          "entries": [
            {{
              "source_term": "ego",
              "target_term": "ایگو",
              "note": "psychological self, not merely pride"
            }}
          ]
        }}
        {extra}
        """
    ).strip()


def build_anchor_extraction_prompt(corpus: str, chapter_count: int, anchor_max_terms: int) -> str:
    return textwrap.dedent(
        f"""
        Build the initial concept anchor glossary from this source material.
        The sample spans {chapter_count} chapter(s).
        Return up to {anchor_max_terms} core anchors and a few short global notes.

        Source corpus:
        {corpus}
        """
    ).strip()


def request_json_object(
    client: OpenAI,
    model: str,
    instructions: str,
    prompt: str,
    max_output_tokens: int,
    retries: int,
    purpose: str,
) -> dict:
    feedback = ""
    last_error = f"Failed to {purpose}."

    for attempt in range(1, retries + 1):
        response = client.responses.create(
            model=model,
            instructions=instructions,
            max_output_tokens=max_output_tokens,
            input=build_user_text_input(f"{prompt}\n\n{feedback}".strip()),
        )

        output_text = clean_model_output(response.output_text)
        status = getattr(response, "status", None)
        if status and status != "completed":
            last_error = f"Model status was {status}: {getattr(response, 'incomplete_details', None)}"
            feedback = "Return one complete JSON object only."
            time.sleep(attempt * 2)
            continue

        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError as exc:
            last_error = str(exc)
            feedback = (
                "Your previous answer was not valid JSON. Return one JSON object only, with no "
                "markdown fences and no explanatory prose."
            )
            time.sleep(attempt * 2)
            continue

        if not isinstance(parsed, dict):
            last_error = "The response JSON was not an object."
            feedback = "Return a JSON object, not an array or a string."
            time.sleep(attempt * 2)
            continue

        return parsed

    raise RuntimeError(f"{purpose.capitalize()} failed: {last_error}")


def normalize_anchor_entries(entries: object, anchor_max_terms: int) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()

    if not isinstance(entries, list):
        return normalized

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source_term = str(entry.get("source_term", "")).strip()
        target_term = str(entry.get("target_term", "")).strip()
        note = str(entry.get("note", "")).strip()
        key = source_term.casefold()
        if not source_term or not target_term or key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "source_term": source_term,
                "target_term": target_term,
                "note": note,
            }
        )
        if len(normalized) >= anchor_max_terms:
            break

    normalized.sort(key=lambda item: item["source_term"].casefold())
    return normalized


def normalize_string_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def normalize_int_list(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(number for number in result if number > 0))


def merge_anchor_updates(state: dict, payload: dict) -> dict:
    merged_entries = {
        entry["source_term"].casefold(): dict(entry)
        for entry in state.get("entries", [])
    }
    for entry in normalize_anchor_entries(payload.get("entries", []), state["anchor_max_terms"]):
        merged_entries[entry["source_term"].casefold()] = entry

    merged_notes = normalize_string_list(
        [*state.get("global_notes", []), *normalize_string_list(payload.get("global_notes", []))]
    )

    state["entries"] = sorted(merged_entries.values(), key=lambda item: item["source_term"].casefold())[
        : state["anchor_max_terms"]
    ]
    state["global_notes"] = merged_notes
    return state


def format_anchor_state_for_prompt(state: dict) -> str:
    entries = state.get("entries", [])
    notes = state.get("global_notes", [])
    if not entries and not notes:
        return "No concept anchors are currently available."

    lines = ["Concept anchors to keep consistent across the whole book:"]
    if notes:
        lines.append("Global notes:")
        for note in notes:
            lines.append(f"- {note}")
    if entries:
        lines.append("Anchored terms:")
        for entry in entries:
            line = f"- {entry['source_term']} -> {entry['target_term']}"
            if entry.get("note"):
                line = f"{line} ({entry['note']})"
            lines.append(line)
    return "\n".join(lines)


def build_user_text_input(text: str) -> list[dict[str, object]]:
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": text,
                }
            ],
        }
    ]


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def unpack_epub(input_epub: Path, extracted_dir: Path) -> None:
    extracted_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(input_epub, "r") as archive:
        archive.extractall(extracted_dir)


def find_opf_path(extracted_dir: Path) -> Path:
    container_path = extracted_dir / "META-INF" / "container.xml"
    tree = parse_xml_file(container_path)
    rootfile = tree.xpath(
        "string(/container:container/container:rootfiles/container:rootfile/@full-path)",
        namespaces=CONTAINER_NS,
    )
    if not rootfile:
        raise RuntimeError("Could not find OPF path in META-INF/container.xml.")
    return extracted_dir / Path(rootfile)


def parse_xml_file(path: Path) -> etree._ElementTree:
    parser = etree.XMLParser(remove_blank_text=False, recover=False, resolve_entities=False)
    return etree.parse(str(path), parser)


def collect_translation_targets(opf_tree: etree._ElementTree, opf_path: Path) -> list[ChapterTarget]:
    manifest_items: dict[str, tuple[str, str, str]] = {}
    for item in opf_tree.xpath("/opf:package/opf:manifest/opf:item", namespaces=OPF_NS):
        item_id = item.get("id")
        href = item.get("href")
        media_type = item.get("media-type", "")
        properties = item.get("properties", "")
        if item_id and href:
            manifest_items[item_id] = (href, media_type, properties)

    targets: list[ChapterTarget] = []
    seen: set[str] = set()
    index = 1

    for itemref in opf_tree.xpath("/opf:package/opf:spine/opf:itemref", namespaces=OPF_NS):
        idref = itemref.get("idref")
        if not idref or idref not in manifest_items:
            continue
        href, media_type, _properties = manifest_items[idref]
        if media_type not in XHTML_MEDIA_TYPES:
            continue
        relative_path = normalize_href(href)
        absolute_path = opf_path.parent / Path(relative_path)
        if relative_path in seen:
            continue
        seen.add(relative_path)
        targets.append(
            ChapterTarget(
                index=index,
                label=absolute_path.name,
                relative_path=relative_path,
                absolute_path=absolute_path,
            )
        )
        index += 1

    for _item_id, (href, media_type, properties) in manifest_items.items():
        if media_type not in XHTML_MEDIA_TYPES or "nav" not in properties.split():
            continue
        relative_path = normalize_href(href)
        if relative_path in seen:
            continue
        absolute_path = opf_path.parent / Path(relative_path)
        targets.append(
            ChapterTarget(
                index=index,
                label=f"nav-{absolute_path.name}",
                relative_path=relative_path,
                absolute_path=absolute_path,
            )
        )
        index += 1

    return targets


def normalize_href(href: str) -> str:
    return posixpath.normpath(href)


def resolve_vazirmatn_bytes(font_path: Path | None, font_url: str | None) -> bytes:
    if font_path:
        data = font_path.read_bytes()
        validate_font_bytes(data, source=str(font_path))
        return data

    candidates = [candidate for candidate in [font_url, *FONT_DOWNLOAD_CANDIDATES] if candidate]
    errors: list[str] = []
    for candidate in candidates:
        try:
            request = Request(candidate, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=60) as response:
                data = response.read()
            validate_font_bytes(data, source=candidate)
            return data
        except (HTTPError, URLError, ValueError) as exc:
            errors.append(f"{candidate}: {exc}")

    joined = "\n".join(errors) if errors else "No download candidates were configured."
    raise RuntimeError(
        "Could not obtain Vazirmatn-Regular.ttf. Supply --font-path or --font-url.\n"
        f"{joined}"
    )


def validate_font_bytes(data: bytes, source: str) -> None:
    if len(data) < 12:
        raise ValueError(f"Downloaded font from {source} is too small.")
    if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"true"):
        raise ValueError(f"Downloaded file from {source} does not look like a TTF/OTF font.")


def install_assets(book_root: Path, font_bytes: bytes) -> None:
    css_path = book_root / CSS_RELATIVE_PATH
    font_path = book_root / FONT_RELATIVE_PATH
    css_path.parent.mkdir(parents=True, exist_ok=True)
    font_path.parent.mkdir(parents=True, exist_ok=True)

    font_path.write_bytes(font_bytes)
    css_path.write_text(build_css(), encoding="utf-8")


def build_css() -> str:
    return textwrap.dedent(
        """
        @font-face {
            font-family: "Vazirmatn";
            src: url("../fonts/Vazirmatn-Regular.ttf") format("truetype");
            font-weight: 400;
            font-style: normal;
        }

        html,
        body {
            direction: rtl;
            font-family: "Vazirmatn", serif;
            line-height: 1.9;
            text-align: justify;
            hanging-punctuation: first last;
            widows: 2;
            orphans: 2;
        }

        body {
            margin: 5%;
        }

        h1,
        h2,
        h3,
        h4,
        h5,
        h6 {
            direction: rtl;
            text-align: right;
            line-height: 1.5;
        }

        p,
        li,
        blockquote,
        div {
            direction: rtl;
            text-align: justify;
        }

        img,
        svg,
        figure {
            direction: ltr;
        }
        """
    ).strip() + "\n"


def ensure_manifest_assets(opf_tree: etree._ElementTree) -> None:
    manifest = opf_tree.xpath("/opf:package/opf:manifest", namespaces=OPF_NS)
    if not manifest:
        raise RuntimeError("EPUB OPF package is missing a manifest.")
    manifest_el = manifest[0]

    existing_hrefs = {
        normalize_href(item.get("href", "")): item
        for item in manifest_el.xpath("./opf:item", namespaces=OPF_NS)
    }
    for href, item_id, media_type in (
        (CSS_RELATIVE_PATH, "persian-rtl-css", "text/css"),
        (FONT_RELATIVE_PATH, "vazirmatn-regular", "font/ttf"),
    ):
        normalized_href = posixpath.relpath(href, ".")
        if normalized_href in existing_hrefs:
            continue
        item = etree.SubElement(manifest_el, f"{{{OPF_NS['opf']}}}item")
        item.set("id", unique_manifest_id(manifest_el, item_id))
        item.set("href", href)
        item.set("media-type", media_type)


def unique_manifest_id(manifest_el: etree._Element, base_id: str) -> str:
    existing = {item.get("id") for item in manifest_el if item.get("id")}
    if base_id not in existing:
        return base_id
    counter = 2
    while f"{base_id}-{counter}" in existing:
        counter += 1
    return f"{base_id}-{counter}"


def update_package_language(opf_tree: etree._ElementTree) -> None:
    metadata = opf_tree.xpath("/opf:package/opf:metadata", namespaces=OPF_NS)
    if not metadata:
        raise RuntimeError("EPUB OPF package is missing metadata.")
    metadata_el = metadata[0]
    languages = metadata_el.xpath("./dc:language", namespaces=OPF_NS)
    if languages:
        for language in languages:
            language.text = "fa"
        return

    language_el = etree.SubElement(metadata_el, f"{{{OPF_NS['dc']}}}language")
    language_el.text = "fa"


def process_chapter(
    client: OpenAI,
    chapter: ChapterTarget,
    total_chapters: int,
    model: str,
    book_context: str,
    translation_context: str,
    work_dir: Path,
    force: bool,
    max_output_tokens: int,
    retries: int,
    anchor_state: dict,
    anchor_state_path: Path,
    anchor_markdown_path: Path,
    skip_concept_anchoring: bool,
    anchor_review_interval: int,
) -> dict:
    print(f"[{chapter.index}/{total_chapters}] Translating {chapter.relative_path}...")
    cache_path = work_dir / f"{chapter.index:03d}-{safe_slug(chapter.label)}.translated.xhtml"
    source_cache_path = work_dir / f"{chapter.index:03d}-{safe_slug(chapter.label)}.source.xhtml"

    original_xhtml = read_xmlish_text(chapter.absolute_path)
    source_cache_path.write_text(original_xhtml, encoding="utf-8")

    if cache_path.exists() and not force:
        translated_xhtml = cache_path.read_text(encoding="utf-8")
        print("  Reusing cached translated chapter.")
    else:
        translated_xhtml = translate_chapter(
            client=client,
            model=model,
            chapter=chapter,
            original_xhtml=original_xhtml,
            book_context=book_context,
            translation_context=translation_context,
            max_output_tokens=max_output_tokens,
            retries=retries,
            anchor_state=anchor_state,
        )
        cache_path.write_text(translated_xhtml, encoding="utf-8")

    css_href = relative_href_from_chapter(chapter.relative_path, CSS_RELATIVE_PATH)
    finalized_xhtml = enforce_persian_xhtml_defaults(translated_xhtml, css_href)
    chapter.absolute_path.write_text(finalized_xhtml, encoding="utf-8")
    cache_path.write_text(finalized_xhtml, encoding="utf-8")

    if skip_concept_anchoring:
        return anchor_state

    if chapter.index not in set(anchor_state.get("translated_chapters", [])):
        anchor_state = update_concept_anchors_for_chapter(
            client=client,
            model=model,
            chapter=chapter,
            anchor_state=anchor_state,
            translation_context=translation_context,
            source_xhtml=original_xhtml,
            translated_xhtml=finalized_xhtml,
            max_output_tokens=max_output_tokens,
            retries=retries,
        )
        anchor_state["translated_chapters"] = sorted(
            set(anchor_state.get("translated_chapters", [])) | {chapter.index}
        )
        save_anchor_state(anchor_state_path, anchor_markdown_path, anchor_state)

    if anchor_review_interval > 0 and chapter.index % anchor_review_interval == 0:
        if chapter.index > int(anchor_state.get("last_reviewed_chapter", 0)):
            anchor_state = review_anchor_drift(
                client=client,
                model=model,
                work_dir=work_dir,
                anchor_state=anchor_state,
                chapter_index=chapter.index,
                translation_context=translation_context,
                max_output_tokens=max_output_tokens,
                retries=retries,
                review_interval=anchor_review_interval,
            )
            anchor_state["last_reviewed_chapter"] = chapter.index
            save_anchor_state(anchor_state_path, anchor_markdown_path, anchor_state)

    return anchor_state


def read_xmlish_text(path: Path) -> str:
    raw = path.read_bytes()
    encoding_match = re.search(br'encoding=["\']([^"\']+)["\']', raw[:200])
    encodings = []
    if encoding_match:
        encodings.append(encoding_match.group(1).decode("ascii", errors="ignore"))
    encodings.extend(["utf-8", "utf-8-sig", "cp1252"])
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Could not decode chapter file: {path}")


def update_concept_anchors_for_chapter(
    client: OpenAI,
    model: str,
    chapter: ChapterTarget,
    anchor_state: dict,
    translation_context: str,
    source_xhtml: str,
    translated_xhtml: str,
    max_output_tokens: int,
    retries: int,
) -> dict:
    source_text = extract_visible_text_from_xhtml(source_xhtml)[:DEFAULT_CORPUS_CHARS_PER_CHAPTER]
    translated_text = extract_visible_text_from_xhtml(translated_xhtml)[:DEFAULT_CORPUS_CHARS_PER_CHAPTER]
    if not source_text or not translated_text:
        return anchor_state

    payload = request_json_object(
        client=client,
        model=model,
        instructions=build_anchor_update_instructions(
            anchor_max_terms=anchor_state["anchor_max_terms"],
            translation_context=translation_context,
        ),
        prompt=build_anchor_update_prompt(
            chapter=chapter,
            anchor_state=anchor_state,
            source_text=source_text,
            translated_text=translated_text,
        ),
        max_output_tokens=max_output_tokens,
        retries=retries,
        purpose=f"update concept anchors for {chapter.relative_path}",
    )
    return merge_anchor_updates(anchor_state, payload)


def build_anchor_update_instructions(anchor_max_terms: int, translation_context: str) -> str:
    extra = (
        f"\n\nAdditional translation context:\n{translation_context.strip()}"
        if translation_context
        else ""
    )
    return textwrap.dedent(
        f"""
        You are refining a Progressive Concept Anchoring glossary for an English-to-Persian
        translation of a psychological or philosophical book.

        Compare the current glossary with a source chapter and its Persian translation.
        - identify only high-value additions or refinements
        - preserve conceptual consistency across chapters
        - update notes when a term needs nuance control
        - do not flood the glossary with trivial vocabulary
        - return valid JSON only
        - return a compact delta, not the full glossary
        - keep the final glossary compatible with a maximum of {anchor_max_terms} total entries

        JSON schema:
        {{
          "global_notes": ["short note", "..."],
          "entries": [
            {{
              "source_term": "being",
              "target_term": "هستی",
              "note": "ontological sense, not mere existence in a casual sense"
            }}
          ]
        }}
        {extra}
        """
    ).strip()


def build_anchor_update_prompt(
    chapter: ChapterTarget,
    anchor_state: dict,
    source_text: str,
    translated_text: str,
) -> str:
    return textwrap.dedent(
        f"""
        Refine the glossary using this newly translated chapter.

        Current concept anchors:
        {json.dumps(anchor_state, ensure_ascii=False, indent=2)}

        Chapter:
        {chapter.label} ({chapter.relative_path})

        Source text:
        {source_text}

        Persian translation:
        {translated_text}
        """
    ).strip()


def review_anchor_drift(
    client: OpenAI,
    model: str,
    work_dir: Path,
    anchor_state: dict,
    chapter_index: int,
    translation_context: str,
    max_output_tokens: int,
    retries: int,
    review_interval: int,
) -> dict:
    start_index = max(1, chapter_index - review_interval + 1)
    review_blocks: list[str] = []
    for current_index in range(start_index, chapter_index + 1):
        prefix = f"{current_index:03d}-"
        source_files = sorted(work_dir.glob(f"{prefix}*.source.xhtml"))
        translated_files = sorted(work_dir.glob(f"{prefix}*.translated.xhtml"))
        if not source_files or not translated_files:
            continue
        source_text = extract_visible_text_from_xhtml(
            source_files[0].read_text(encoding="utf-8")
        )[:8000]
        translated_text = extract_visible_text_from_xhtml(
            translated_files[0].read_text(encoding="utf-8")
        )[:8000]
        if source_text and translated_text:
            review_blocks.append(
                textwrap.dedent(
                    f"""
                    Chapter {current_index}
                    Source:
                    {source_text}

                    Translation:
                    {translated_text}
                    """
                ).strip()
            )

    if not review_blocks:
        return anchor_state

    print(f"Reviewing concept drift through chapter {chapter_index}...")
    payload = request_json_object(
        client=client,
        model=model,
        instructions=build_anchor_review_instructions(
            anchor_max_terms=anchor_state["anchor_max_terms"],
            translation_context=translation_context,
        ),
        prompt=build_anchor_review_prompt(anchor_state, review_blocks),
        max_output_tokens=max_output_tokens,
        retries=retries,
        purpose="review concept drift",
    )
    return merge_anchor_updates(anchor_state, payload)


def build_anchor_review_instructions(anchor_max_terms: int, translation_context: str) -> str:
    extra = (
        f"\n\nAdditional translation context:\n{translation_context.strip()}"
        if translation_context
        else ""
    )
    return textwrap.dedent(
        f"""
        You are auditing concept drift in an English-to-Persian translation glossary.

        Review recent source/translation chapter pairs and refine the concept anchors when needed.
        - focus on philosophical, psychological, existential, and psychoanalytic terms
        - detect term drift, nuance drift, and avoidable inconsistency
        - suggest only high-value glossary refinements
        - return valid JSON only
        - return a compact delta, not the entire glossary
        - the final glossary must remain within {anchor_max_terms} total entries

        JSON schema:
        {{
          "global_notes": ["short note", "..."],
          "entries": [
            {{
              "source_term": "self",
              "target_term": "خویشتن",
              "note": "core selfhood, not generic personhood"
            }}
          ]
        }}
        {extra}
        """
    ).strip()


def build_anchor_review_prompt(anchor_state: dict, review_blocks: list[str]) -> str:
    review_text = "\n\n".join(review_blocks)
    return textwrap.dedent(
        f"""
        Review the current glossary against these recent chapter pairs and return only the
        glossary updates needed to reduce concept drift.

        Current concept anchors:
        {json.dumps(anchor_state, ensure_ascii=False, indent=2)}

        Recent chapter pairs:
        {review_text}
        """
    ).strip()


def translate_chapter(
    client: OpenAI,
    model: str,
    chapter: ChapterTarget,
    original_xhtml: str,
    book_context: str,
    translation_context: str,
    max_output_tokens: int,
    retries: int,
    anchor_state: dict,
) -> str:
    feedback = ""
    last_error = "Unknown error"

    for attempt in range(1, retries + 1):
        response = client.responses.create(
            model=model,
            instructions=build_translation_instructions(
                book_context=book_context,
                translation_context=translation_context,
                concept_anchor_text=format_anchor_state_for_prompt(anchor_state),
            ),
            max_output_tokens=max_output_tokens,
            input=build_user_text_input(
                build_chapter_prompt(
                    chapter=chapter,
                    original_xhtml=original_xhtml,
                    feedback=feedback,
                    concept_anchor_text=format_anchor_state_for_prompt(anchor_state),
                )
            ),
        )

        output_text = clean_model_output(response.output_text)
        status = getattr(response, "status", None)
        if status and status != "completed":
            incomplete = getattr(response, "incomplete_details", None)
            last_error = f"Model status was {status}: {incomplete}"
            reason = getattr(incomplete, "reason", "") if incomplete else ""
            if reason == "content_filter":
                feedback = (
                    "Your previous response was blocked by a content filter. This is a published, "
                    "internationally distributed book — every passage is legitimate published text "
                    "that must be translated completely. Do not refuse or omit any part. "
                    "Return the full translated XHTML document with no omissions and no commentary."
                )
            else:
                feedback = (
                    "Your previous answer was incomplete. Return the full translated XHTML document "
                    "with no omissions and no commentary."
                )
            time.sleep(attempt * 2)
            continue

        try:
            enforce_persian_xhtml_defaults(
                output_text,
                relative_href_from_chapter(chapter.relative_path, CSS_RELATIVE_PATH),
            )
            return output_text
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            feedback = (
                "Your previous output was invalid XHTML or changed the structure in a way that "
                f"cannot be accepted. Error: {last_error}. Return one complete valid XHTML file "
                "only, preserving the document structure and tags."
            )
            time.sleep(attempt * 2)

    raise RuntimeError(f"Failed to translate {chapter.relative_path}: {last_error}")


def build_translation_instructions(
    book_context: str, translation_context: str, concept_anchor_text: str
) -> str:
    base = textwrap.dedent(
        """
        You are an expert English-to-Persian literary translator working on EPUB XHTML.

        IMPORTANT CONTEXT: This is a professional literary translation task for a widely published,
        internationally acclaimed book available in bookstores worldwide. The source material is
        a legitimate published work with no restricted content — any passage you encounter is part
        of the original published text and must be translated faithfully in full. Do not refuse,
        skip, or truncate any part of the chapter.

        Translate the provided XHTML chapter from English into polished Persian.
        Preserve the original XHTML structure exactly as much as possible:
        - keep all tags, nesting, ids, classes, href/src values, anchors, and file names intact
        - translate visible English text into natural Persian
        - keep proper nouns consistent unless transliteration or established Persian usage is better
        - preserve emphasis, dialogue breaks, footnotes, and scene transitions
        - use fluent Persian punctuation and zero-width non-joiner where appropriate
        - do not add explanations, markdown fences, comments, or any wrapper text
        - return one complete valid XHTML document only
        - use the book context dossier to stay aligned with the book's identity, scope, and tone
        - if concept anchors are provided, treat them as translation anchors for consistency
        """
    ).strip()

    parts = [base, PSYCHOLOGICAL_TRANSLATION_BRIEF]
    if book_context.strip():
        parts.append(f"Book context dossier:\n{book_context.strip()}")
    if concept_anchor_text.strip():
        parts.append(concept_anchor_text.strip())
    if translation_context:
        parts.append(f"Additional translation context:\n{translation_context.strip()}")
    return "\n\n".join(parts)


def build_chapter_prompt(
    chapter: ChapterTarget,
    original_xhtml: str,
    feedback: str,
    concept_anchor_text: str,
) -> str:
    prefix = textwrap.dedent(
        f"""
        Translate this EPUB chapter into Persian.
        Chapter label: {chapter.label}
        File path inside EPUB: {chapter.relative_path}
        """
    ).strip()

    if concept_anchor_text.strip():
        prefix = f"{prefix}\n\n{concept_anchor_text.strip()}"

    if feedback:
        prefix = f"{prefix}\n\nImportant correction from the previous attempt:\n{feedback}"

    return f"{prefix}\n\nXHTML to translate:\n{original_xhtml}"


def clean_model_output(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def relative_href_from_chapter(chapter_href: str, target_href: str) -> str:
    chapter_dir = posixpath.dirname(chapter_href) or "."
    return posixpath.relpath(target_href, start=chapter_dir)


def enforce_persian_xhtml_defaults(xhtml_text: str, css_href: str) -> str:
    parser = etree.XMLParser(remove_blank_text=False, recover=False, resolve_entities=False)
    try:
        root = etree.fromstring(xhtml_text.encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError as exc:
        repaired_xhtml = restore_known_xhtml_namespaces(xhtml_text)
        if repaired_xhtml == xhtml_text:
            raise
        try:
            root = etree.fromstring(repaired_xhtml.encode("utf-8"), parser=parser)
            xhtml_text = repaired_xhtml
        except etree.XMLSyntaxError:
            raise exc

    if etree.QName(root).localname.lower() != "html":
        raise ValueError("Translated chapter root element must be <html>.")

    namespace = etree.QName(root).namespace or XHTML_NS
    head = find_child(root, "head")
    body = find_child(root, "body")
    if head is None or body is None:
        raise ValueError("Translated chapter must contain both <head> and <body>.")

    root.set("lang", "fa")
    root.set(f"{{{XML_NS}}}lang", "fa")
    root.set("dir", "rtl")
    body.set("dir", "rtl")

    existing_class = body.get("class", "")
    classes = {part for part in existing_class.split() if part}
    classes.add("translated-fa")
    body.set("class", " ".join(sorted(classes)))

    if not has_stylesheet_link(head, css_href):
        link = etree.Element(f"{{{namespace}}}link")
        link.set("rel", "stylesheet")
        link.set("type", "text/css")
        link.set("href", css_href)
        head.append(link)

    serialized = etree.tostring(
        root.getroottree(),
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=False,
    )
    return serialized.decode("utf-8")


def restore_known_xhtml_namespaces(xhtml_text: str) -> str:
    repaired = xhtml_text
    repaired = ensure_namespace_declaration(repaired, "epub", EPUB_NS)
    return repaired


def ensure_namespace_declaration(xhtml_text: str, prefix: str, uri: str) -> str:
    if f"{prefix}:" not in xhtml_text:
        return xhtml_text
    if re.search(rf"\bxmlns:{re.escape(prefix)}\s*=", xhtml_text):
        return xhtml_text
    return re.sub(
        r"(<html\b[^>]*)(>)",
        rf'\1 xmlns:{prefix}="{uri}"\2',
        xhtml_text,
        count=1,
        flags=re.IGNORECASE,
    )


def find_child(root: etree._Element, local_name: str) -> etree._Element | None:
    for child in root:
        if etree.QName(child).localname.lower() == local_name.lower():
            return child
    return None


def has_stylesheet_link(head: etree._Element, css_href: str) -> bool:
    for child in head:
        if etree.QName(child).localname.lower() != "link":
            continue
        if child.get("href") == css_href and child.get("rel") == "stylesheet":
            return True
    return False


def write_xml_file(path: Path, tree: etree._ElementTree) -> None:
    tree.write(
        str(path),
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=False,
    )


def build_epub(extracted_dir: Path, output_epub: Path) -> None:
    output_epub.parent.mkdir(parents=True, exist_ok=True)
    mimetype_path = extracted_dir / "mimetype"
    if output_epub.exists():
        output_epub.unlink()

    with zipfile.ZipFile(output_epub, "w") as archive:
        if mimetype_path.exists():
            archive.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
        for path in sorted(extracted_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(extracted_dir).as_posix()
            if relative == "mimetype":
                continue
            archive.write(path, relative, compress_type=zipfile.ZIP_DEFLATED)


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return slug or "chapter"


if __name__ == "__main__":
    raise SystemExit(main())
