from .hook import main_for


def main() -> int:
    return main_for("claude")


if __name__ == "__main__":
    raise SystemExit(main())
