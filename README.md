# EPUB to Persian Translator

Translate an English EPUB or text-based PDF into a polished Persian EPUB chapter by chapter with the OpenAI Responses API and an embedded `Vazirmatn` font.

## Why this approach

- Uses the current OpenAI Python SDK with `client.responses.create(...)`
- Accepts `.epub` and text-based `.pdf` inputs
- Translates chapter files one by one, so runs are easier to resume
- Uses Progressive Concept Anchoring so philosophical terms stay consistent across chapters
- Rebuilds the final book as EPUB after applying Persian RTL styling
- Embeds `Vazirmatn-Regular.ttf` into the EPUB for better Persian rendering

## Requirements

- Python 3.11+
- Either `OPENAI_API_KEY`, or both `AZURE_API_ENDPOINT` and `AZURE_API_KEY`, in `.env` or your environment

## Install

```bash
uv sync
```

## .env

Create a local `.env` file for either OpenAI or Azure OpenAI:

```bash
OPENAI_API_KEY=your_api_key
MODEL=gpt-5.4
```

```bash
AZURE_API_ENDPOINT=https://your-resource.openai.azure.com
AZURE_API_KEY=your_azure_api_key
MODEL=your_azure_deployment_name
```

## Usage

```bash
uv run epub-fa-translator "/path/to/book.epub" "/path/to/book.fa.epub" \
  --context-file "translation-context.example.txt"
```

```bash
uv run epub-fa-translator "/path/to/book.pdf" "/path/to/book.fa.epub" \
  --context-file "translation-context.example.txt"
```

## Notes

- If `OPENAI_API_KEY` is set, the tool uses OpenAI directly.
- If `OPENAI_API_KEY` is not set and both `AZURE_API_ENDPOINT` and `AZURE_API_KEY` are set, the tool uses Azure OpenAI through the same `openai` package.
- The model/deployment name defaults to `MODEL` from `.env` or your environment, and can still be overridden with `--model`.
- If `Vazirmatn` is not available locally, the tool tries official CDN-style download URLs automatically.
- PDF input works best for text-based PDFs. Scanned/image PDFs should be OCRed first.
- Per-chapter checkpoints are stored in `.translator-work/`, so reruns can resume without retranslating finished chapters.
- Progressive Concept Anchoring writes a persistent glossary to `.translator-work/<book>/concept-anchors.json`.
- The glossary is built first, injected into each chapter translation, and refined as more chapters are translated.
- A periodic drift review runs every few chapters by default to catch philosophical term drift.
- Use `--force` if you want to ignore cached chapter translations and translate everything again.
- The default prompt is tuned for deep psychological and concept-heavy books.
- Add your own translator guidance with `--context` or `--context-file`.

