{ pkgs, python }:

python.override {
  self = python;
  packageOverrides = pyfinal: pyprev: {
    xdg-base-dirs = pyfinal.buildPythonPackage rec {
      pname = "xdg-base-dirs";
      version = "6.0.2";
      pyproject = true;

      src = pyfinal.fetchPypi {
        pname = "xdg_base_dirs";
        inherit version;
        hash = "sha256-lQUE4U0nzzycs3dEaApDvwrELv78TvSs+Y3HNsqyvO0=";
      };

      build-system = [ pyfinal.poetry-core ];

      pythonImportsCheck = [ "xdg_base_dirs" ];
      doCheck = false;
    };

    textual-serve = pyfinal.buildPythonPackage rec {
      pname = "textual-serve";
      version = "1.1.3";
      pyproject = true;

      src = pyfinal.fetchPypi {
        pname = "textual_serve";
        inherit version;
        hash = "sha256-+PY2ri9f1lG3nZZUc8PpOD01Ic34lvm8KJcJGF2j9oM=";
      };

      build-system = [ pyfinal.hatchling ];

      dependencies = [
        pyfinal.aiohttp
        pyfinal.aiohttp-jinja2
        pyfinal.jinja2
        pyfinal.rich
        pyfinal.textual
      ];

      pythonImportsCheck = [ "textual_serve" ];
      doCheck = false;
    };

    notify-py = pyfinal.buildPythonPackage rec {
      pname = "notify-py";
      version = "0.3.43";
      pyproject = true;

      src = pyfinal.fetchPypi {
        pname = "notify_py";
        inherit version;
        hash = "sha256-Fu4UbUjxa65drSM9tmAUo4fv0sbtLEyvHgiu9DIHBRM=";
      };

      build-system = [ pyfinal.poetry-core ];

      # Upstream pins loguru<=0.6.0 but nixpkgs only ships 0.7.x; the API used
      # by notify-py has not changed across the 0.6 → 0.7 boundary.
      pythonRelaxDeps = [ "loguru" ];

      dependencies = [
        pyfinal.loguru
      ] ++ pyfinal.lib.optionals pyfinal.stdenv.isLinux [
        pyfinal.jeepney
      ];

      pythonImportsCheck = [ "notifypy" ];
      doCheck = false;
    };

    textual-speedups = pyfinal.buildPythonPackage rec {
      pname = "textual-speedups";
      version = "0.2.1";
      pyproject = true;

      src = pyfinal.fetchPypi {
        pname = "textual_speedups";
        inherit version;
        hash = "sha256-cs8Pe97t4BU2e1m3C89yS6LDCAqGQevF65SzatFTaCQ=";
      };

      cargoDeps = pkgs.rustPlatform.fetchCargoVendor {
        inherit src;
        name = "${pname}-${version}";
        hash = "sha256-Bz4ocEziOlOX4z5F9EDry99YofeGyxL/6OTIf/WEgK4=";
      };

      nativeBuildInputs = [
        pkgs.rustPlatform.cargoSetupHook
        pkgs.rustPlatform.maturinBuildHook
        pkgs.cargo
        pkgs.rustc
      ];

      pythonImportsCheck = [ "textual_speedups" ];
      doCheck = false;
    };
  };
}
