import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def main():
    if len(sys.argv) < 2:
        raise SystemExit(
            "Использование: python run_selected_regions_pipeline.py <region_code_1> [region_code_2 ...]"
        )

    region_codes = sys.argv[1:]
    pipeline_script = BASE_DIR / "scripts" / "run_region_pipeline.py"

    for region_code in region_codes:
        print(f"=== START {region_code} ===")
        subprocess.run([sys.executable, str(pipeline_script), region_code], check=True)
        print(f"=== DONE {region_code} ===")


if __name__ == "__main__":
    main()