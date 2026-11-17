{
  description = "paperboat: a personal paper collector with a web frontend";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      pythonFor = pkgs: pkgs.python3.withPackages (ps: with ps; [
        fastapi
        uvicorn
        httpx
        pymupdf
        python-multipart
        pytest
      ]);
    in {
      packages = forAll (pkgs: rec {
        paperboat = pkgs.writeShellApplication {
          name = "paperboat";
          runtimeInputs = [ (pythonFor pkgs) ];
          text = ''
            export PYTHONPATH="${self}''${PYTHONPATH:+:$PYTHONPATH}"
            exec python -m paperboat "$@"
          '';
        };
        default = paperboat;
      });

      apps = forAll (pkgs: {
        default = { type = "app"; program = "${self.packages.${pkgs.system}.paperboat}/bin/paperboat"; };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell { packages = [ (pythonFor pkgs) ]; };
      });
    };
}
