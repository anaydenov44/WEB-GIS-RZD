import sys

from pipeline_utils import cleanup_region_files, load_regions_registry


def main():
    if len(sys.argv) == 2:
        region_code = sys.argv[1]
        deleted = cleanup_region_files(region_code)
        print(f"[{region_code}] Удалено файлов: {len(deleted)}")
        for item in deleted:
            print(item)
        return

    registry = load_regions_registry()
    total_deleted = 0

    for region in registry["regions"]:
        region_code = region["code"]
        deleted = cleanup_region_files(region_code)
        total_deleted += len(deleted)
        print(f"[{region_code}] Удалено файлов: {len(deleted)}")
        for item in deleted:
            print(f"  {item}")

    print(f"Итого удалено файлов: {total_deleted}")


if __name__ == "__main__":
    main()