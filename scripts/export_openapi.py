"""Export Python schemas and validate the Node.js/Go contract snapshots offline."""
import importlib
import json
from pathlib import Path

SERVICES = ("users", "catalog", "schedule", "bookings", "pricing", "payments", "sessions", "notifications")
PYTHON_SERVICES = {"users", "catalog", "pricing", "notifications"}
GO_SERVICES = {"bookings", "payments"}


def main():
    target = Path(__file__).resolve().parents[1] / "docs" / "openapi"
    target.mkdir(parents=True, exist_ok=True)
    for name in SERVICES:
        path = target / f"{name}.json"
        if name in PYTHON_SERVICES:
            module = importlib.import_module(f"services.{name}.main")
            schema = module.app.openapi()
            schema["servers"] = [{"url": f"http://localhost:{8001 + SERVICES.index(name)}"}]
            path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
        else:
            # These contracts are the stable API boundary used by the ports.
            schema = json.loads(path.read_text())
            if name in GO_SERVICES:
                embedded = target.parents[1] / "services" / name / "openapi.json"
                if json.loads(embedded.read_text()) != schema:
                    raise ValueError(f"{name}: embedded OpenAPI differs from {path}")
        print(f"{name}: {sum(len(path) for path in schema['paths'].values())} operations")


if __name__ == "__main__":
    main()
