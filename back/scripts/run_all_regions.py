import subprocess
import sys

from pipeline_utils import BASE_DIR, load_regions_registry


def main():
    registry = load_regions_registry()
    pipeline_script = BASE_DIR / "scripts" / "run_region_pipeline.py"

    for region in registry["regions"]:
        region_code = region["code"]
        print(f"=== START {region_code} ===")
        subprocess.run([sys.executable, str(pipeline_script), region_code], check=True)
        print(f"=== DONE {region_code} ===")


if __name__ == "__main__":
    main()