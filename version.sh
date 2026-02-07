#!/bin/bash
# Display version information for the home-server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VERSION=$(cat VERSION 2>/dev/null || echo "unknown")
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
GIT_DATE=$(git log -1 --format=%cd --date=format:'%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")

echo "Version: $VERSION"
echo "Commit:  $GIT_SHA"
echo "Date:    $GIT_DATE"
