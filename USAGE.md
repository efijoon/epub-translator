# for running on the local machine
uv run epub-fa-translator "denial-of-death.pdf" "book.epub" --context-file "translation-context.txt" --anchor-scan-chapters 0 --anchor-max-terms 120 --anchor-review-interval 3

# for running on the production server
# put MODEL=... in .env first
./run_translation.sh "/root/epub-translator/The-Power-Of-Now-EckhartTolle.pdf" "/root/epub-translator/The-Power-Of-Now-EckhartTolle-fa.epub"