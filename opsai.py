from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


if __name__ == "__main__":
    cli_path = Path(__file__).resolve().parent / "opsai" / "cli.py"
    spec = spec_from_file_location("opsai_cli_entry", cli_path)
    if spec is None or spec.loader is None:
        raise SystemExit("错误: 无法加载 opsai/cli.py")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    raise SystemExit(module.main())
