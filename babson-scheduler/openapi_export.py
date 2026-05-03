"""
Export the live FastAPI OpenAPI spec to openapi.json.
Upload this file to IBM Orchestrate as a custom extension.

Usage:
    python openapi_export.py
"""

import json
import os
import sys

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from app.main import app

if __name__ == "__main__":
    spec = app.openapi()
    output_path = os.path.join(os.path.dirname(__file__), "openapi.json")
    with open(output_path, "w") as f:
        json.dump(spec, f, indent=2)
    print(f"OpenAPI spec written to: {output_path}")
    print(f"  Title:   {spec['info']['title']}")
    print(f"  Version: {spec['info']['version']}")
    print(f"  Paths:   {len(spec.get('paths', {}))}")
