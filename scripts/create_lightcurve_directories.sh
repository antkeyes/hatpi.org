#!/bin/bash

# This script:
# - Traverses the source directory containing Gaia .epd.tfa.fits files.
# - Extracts the Gaia ID from each filename.
# - Creates a nested directory structure using the first 16 digits of the Gaia ID.
# - Leaves the last 3 digits out of the directory structure so that multiple files share the same leaf directory.
# - Creates a symlink in the new structure pointing to the original file.

# Source directory containing .epd.tfa.fits files
source_dir="/nfs/php1/ar1/P/PROJ/abodi/lctest/TFALC/aperphot/ihu01/P1200-8400_0013/20230107-20230702"

# Output base directory for symlinks
output_base="/nfs/hatops/ar0/hatpi-landing-page/lightcurves"

# Find all .epd.tfa.fits files
find "$source_dir" -type f -name "*.epd.tfa.fits" -readable 2>/dev/null | while read -r file; do
    filename=$(basename "$file")

    # Example filename: Gaia-DR2-5782870930866323840.epd.tfa.fits
    # Remove prefix "Gaia-DR2-" and suffix ".epd.tfa.fits" to isolate the Gaia ID
    gaia_id=$(echo "$filename" | sed 's/^Gaia-DR2-//' | sed 's/.epd.tfa.fits$//')

    # gaia_id is something like 5782870930866323840 (19 digits)
    # We use the first 16 digits for directories (8 pairs of digits)
    # and leave the last 3 digits out of the directory structure.
    short_id=$(echo "$gaia_id" | cut -c1-16)  # First 16 digits

    # Create the directory path by splitting every two digits with a slash
    # For example, 5782870930866323 -> 57/82/87/09/30/86/63/23/
    dir_path="$output_base/$(echo "$short_id" | sed 's/\(..\)/\1\//g')"

    # Create the directory structure
    mkdir -p "$dir_path"

    # Create the symlink in the final directory
    # The symlink will have the same filename as the original file
    symlink_path="${dir_path}${filename}"

    if [ -e "$symlink_path" ]; then
        echo "Symlink already exists: $symlink_path"
    else
        ln -s "$file" "$symlink_path"
        echo "Symlink created: $symlink_path -> $file"
    fi
done
