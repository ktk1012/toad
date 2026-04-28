{ pkgs, system, python }:

pkgs.mkShellNoCC {
  packages = [
    python
    pkgs.uv
    pkgs.ruff
    pkgs.gnumake
  ];

  env = {
    UV_PYTHON = "${python}/bin/python3.14";
    UV_PYTHON_DOWNLOADS = "never";
  };

  shellHook = ''
    echo "toad dev shell"
    echo "  Python: $(python3 --version)"
    echo "  uv:     $(uv --version)"
  '';
}
