"""Воспроизводимые спецификации практики 6, без запуска внешних систем."""
import importlib
import json
from pathlib import Path

SERVICES = ("users", "catalog", "schedule", "bookings", "pricing", "payments", "sessions", "notifications")


def main():
    target = Path(__file__).resolve().parents[1] / "docs" / "openapi"
    target.mkdir(parents=True, exist_ok=True)
    for name in SERVICES:
        module = importlib.import_module(f"services.{name}.main")
        schema = module.app.openapi()
        schema["servers"] = [{"url": f"http://localhost:{8001 + SERVICES.index(name)}"}]
        (target / f"{name}.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
        print(f"{name}: {sum(len(path) for path in schema['paths'].values())} operations")


if __name__ == "__main__":
    main()
