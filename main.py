"""Windows source and PyInstaller entry point."""

import sys


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--apply-update":
        from scripts.update_installer import main
        main(sys.argv[2])
    else:
        from xigpet.app import main
        main()
