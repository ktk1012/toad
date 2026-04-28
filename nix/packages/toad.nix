{ pkgs, python }:

let
  py = python.pkgs;
in
py.buildPythonApplication {
  pname = "batrachian-toad";
  version = "0.6.14";
  pyproject = true;

  src = ../..;

  build-system = [ py.hatchling ];

  # pyproject.toml pins `hatchling==1.28.0` but nixpkgs ships a newer patch
  # release. Strip the pin so the build uses whatever hatchling is in scope.
  postPatch = ''
    substituteInPlace pyproject.toml \
      --replace-fail 'hatchling==1.28.0' 'hatchling'
  '';

  pythonRelaxDeps = [
    # nixpkgs ships slightly older patch versions; APIs are stable.
    "platformdirs"
    "aiosqlite"
  ];

  dependencies = with py; [
    aiosqlite
    bashlex
    click
    httpx
    notify-py
    packaging
    pathspec
    platformdirs
    psutil
    pyperclip
    rich
    setproctitle
    textual
    textual-serve
    textual-speedups
    tree-sitter
    typeguard
    watchdog
    websockets
    xdg-base-dirs
  ];

  # toad has no test suite shipped with the source tree.
  doCheck = false;

  pythonImportsCheck = [ "toad" "toad.cli" ];

  meta = {
    description = "A unified experience for AI in your terminal.";
    mainProgram = "toad";
    license = pkgs.lib.licenses.mit;
    platforms = pkgs.lib.platforms.unix;
  };
}
