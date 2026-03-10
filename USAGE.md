# for running on the local machine
uv run epub-fa-translator "denial-of-death.pdf" "book.epub" --model "gpt-5.4" --context-file "translation-context.txt" --anchor-scan-chapters 0 --anchor-max-terms 120 --anchor-review-interval 3

# for running on the production server
./run_translation.sh "/path/to/input.pdf" "/path/to/output.epub"