"""Rebuild dashboard.html from dashboard_template.html + results.json."""
import json

with open("results.json") as f:
    results_json = f.read()
    json.loads(results_json)  # fail loudly if results.json is malformed

with open("dashboard_template.html") as f:
    template = f.read()

if "__RESULTS_JSON__" not in template:
    raise SystemExit("dashboard_template.html is missing the __RESULTS_JSON__ placeholder")

with open("dashboard.html", "w") as f:
    f.write(template.replace("__RESULTS_JSON__", results_json))

print(f"dashboard.html rebuilt with {len(results_json)} bytes of results")
