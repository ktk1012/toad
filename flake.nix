{
  description = "toad — a unified experience for AI in your terminal";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      supportedSystems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "x86_64-linux"
        "aarch64-linux"
      ];
      forEachSystem =
        f:
        nixpkgs.lib.genAttrs supportedSystems (
          system:
          let
            pkgs = nixpkgs.legacyPackages.${system};
            python = import ./nix/packages/python-deps.nix {
              inherit pkgs;
              python = pkgs.python314;
            };
          in
          f { inherit pkgs system python; }
        );
    in
    {
      packages = forEachSystem (
        { pkgs, system, python }:
        {
          default = import ./nix/packages/toad.nix { inherit pkgs python; };
          toad = import ./nix/packages/toad.nix { inherit pkgs python; };
        }
      );

      devShells = forEachSystem (
        { pkgs, system, python }:
        {
          default = import ./nix/devshells/default.nix { inherit pkgs system python; };
        }
      );
    };
}
