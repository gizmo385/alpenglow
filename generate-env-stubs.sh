#!/bin/bash
# Generate .env.example stub files from existing .env files
# These stubs contain only variable names without values and can be safely committed

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find all .env files (but not .env.example files)
find "$SCRIPT_DIR" -name ".*env" ! -name "*.example" 2>/dev/null | while read -r env_file; do
    # Skip if it's already an example file
    if [[ "$env_file" == *".example" ]]; then
        continue
    fi

    stub_file="${env_file}.example"

    echo "Processing: $env_file -> $stub_file"

    # Create stub file with empty values
    > "$stub_file"

    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip empty lines
        if [[ -z "$line" ]]; then
            echo "" >> "$stub_file"
            continue
        fi

        # Preserve comments as-is
        if [[ "$line" =~ ^[[:space:]]*# ]]; then
            echo "$line" >> "$stub_file"
            continue
        fi

        # Extract variable name (everything before the first =)
        if [[ "$line" =~ ^([^=]+)= ]]; then
            var_name="${BASH_REMATCH[1]}"
            echo "${var_name}=" >> "$stub_file"
        else
            # Line doesn't match expected format, preserve it
            echo "$line" >> "$stub_file"
        fi
    done < "$env_file"

    echo "Created: $stub_file"
done

echo "Done! Remember to commit the .env.example files."
