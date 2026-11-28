# The module on a NixOS VM, offline: stand-ins for op and for Google, and
# synthetic values only. It shows the unit's paths and permissions, the
# environment file reaching op, and who the socket lets in.
{ self }:
{ pkgs, ... }:
let
  socket = "/run/hermes-gmail/gate.sock";

  # Hands out a synthetic OAuth document, and only when the unit's
  # environment file reached it.
  fakeOp = pkgs.writeShellScriptBin "op" ''
    [ "$OP_SERVICE_ACCOUNT_TOKEN" = synthetic-service-account-token ] || exit 1
    [ "$*" = "document get synthetic-item --vault synthetic-vault" ] || exit 1
    echo '{"client_id": "synthetic-client", "client_secret": "synthetic-secret", "refresh_token": "synthetic-refresh"}'
  '';

  # Google on loopback: the token endpoint and the profile read.
  fakeGoogle = pkgs.writeText "fake-google.py" ''
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer


    class Handler(BaseHTTPRequestHandler):
        def answer(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path != "/token":
                return self.answer(404, {})
            self.answer(200, {"access_token": "synthetic-access", "expires_in": 3600})

        def do_GET(self):
            if self.path != "/gmail/v1/users/me/profile":
                return self.answer(404, {})
            if self.headers.get("Authorization") != "Bearer synthetic-access":
                return self.answer(401, {})
            self.answer(200, {"emailAddress": "owner@example.test", "historyId": "42"})


    HTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
  '';
in
{
  name = "hermes-gmail-module";

  nodes.machine = {
    imports = [ self.nixosModules.default ];

    users.users = {
      # A caller in the socket's group.
      agent = {
        isNormalUser = true;
        extraGroups = [ "hermes-gmail" ];
      };
      # In the group, so it reaches the socket, but not a caller.
      stranger = {
        isNormalUser = true;
        extraGroups = [ "hermes-gmail" ];
      };
      # Not in the group: the socket directory is closed to it.
      outsider.isNormalUser = true;
    };

    environment.etc."hermes-gmail.env" = {
      text = "OP_SERVICE_ACCOUNT_TOKEN=synthetic-service-account-token\n";
      mode = "0400";
    };

    systemd.services.fake-google = {
      wantedBy = [ "multi-user.target" ];
      before = [ "hermes-gmail.service" ];
      serviceConfig = {
        ExecStart = "${pkgs.python3.interpreter} ${fakeGoogle}";
        DynamicUser = true;
      };
    };

    services.hermes-gmail = {
      enable = true;
      opPackage = fakeOp;
      environmentFile = "/etc/hermes-gmail.env";
      settings = {
        account = "owner@example.test";
        vault = {
          item = "synthetic-item";
          vault = "synthetic-vault";
        };
        google = {
          gmail_base = "http://127.0.0.1:8099";
          token_url = "http://127.0.0.1:8099/token";
        };
      };
      callers.agent = {
        user = "agent";
        roles = [ "read" ];
      };
    };

    environment.systemPackages = [ self.packages.${pkgs.stdenv.hostPlatform.system}.default ];
  };

  testScript = ''
    import json


    def profile(user):
        status, output = machine.execute(
            f"runuser -u {user} -- hermes-gmail profile --socket ${socket} 2>&1"
        )
        return status, json.loads(output)


    def owner_and_mode(path):
        return machine.succeed(f"stat -c '%U %G %a' {path}").strip()


    machine.wait_for_unit("fake-google.service")
    machine.wait_for_open_port(8099)
    machine.wait_for_unit("hermes-gmail.service")
    machine.wait_for_file("${socket}")

    with subtest("the unit's directories and socket"):
        assert owner_and_mode("/var/lib/hermes-gmail") == "hermes-gmail hermes-gmail 700"
        assert owner_and_mode("/run/hermes-gmail") == "hermes-gmail hermes-gmail 750"
        assert owner_and_mode("${socket}") == "hermes-gmail hermes-gmail 660"

    with subtest("a caller reads, through op and the token exchange"):
        status, result = profile("agent")
        assert status == 0, result
        assert result == {"account": "owner@example.test", "history_id": "42"}, result

    with subtest("the socket's group is not enough to be a caller"):
        status, error = profile("stranger")
        assert status == 1 and error["category"] == "refused", error

    with subtest("outside the group the socket is out of reach"):
        status, error = profile("outsider")
        assert status == 1 and error["category"] == "gmail_failed", error
        assert "not reachable" in error["detail"], error

    with subtest("a caller cannot read the state directory"):
        machine.fail("runuser -u agent -- ls /var/lib/hermes-gmail")

    with subtest("a restart serves again on a fresh socket"):
        machine.succeed("systemctl restart hermes-gmail.service")
        machine.wait_for_file("${socket}")
        status, result = profile("agent")
        assert status == 0, result
  '';
}
