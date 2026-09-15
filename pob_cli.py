import asyncio
import json
import os
import sys
import subprocess
from pathlib import Path
import argparse

# --- Constants ---
# Determine the base directory of the script to make paths relative
SCRIPT_DIR = Path(__file__).parent.resolve()
# We assume this script lives in the root of the 'uberpob' project directory
UBERPOB_DIR = SCRIPT_DIR
POB_FORK_PATH = UBERPOB_DIR / "pob-mcp" / "PathOfBuilding" / "src"
POB_CMD = "luajit"
POB_ARGS = ["HeadlessWrapper.lua"]

class PoBCLI:
    """
    An asyncio-based Python client for the Path of Building Headless Lua Bridge.
    This class communicates with the Lua process over stdio.
    """

    def __init__(self, cmd, args, cwd):
        self.cmd = cmd
        self.args = args
        self.cwd = cwd
        self.proc = None

    async def start(self):
        """
        Starts the luajit subprocess and waits for it to be ready.
        """
        if self.proc:
            return

        # Replicate the environment variables set by the Node.js bridge
        pob_fork_path_str = str(self.cwd)
        base_dir = pob_fork_path_str.rsplit(os.path.sep + 'src', 1)[0]
        runtime_dir = os.path.join(base_dir, 'runtime')
        runtime_lua_path = os.path.join(runtime_dir, 'lua')
        
        is_windows = sys.platform == 'win32'
        path_sep = ';' if is_windows else ':'
        lua_ext = 'dll' if is_windows else 'so'

        env = os.environ.copy()
        env["POB_API_STDIO"] = "1"
        env["LUA_PATH"] = f"{runtime_lua_path}{os.path.sep}?.lua{path_sep}{runtime_lua_path}{os.path.sep}?{os.path.sep}init.lua{path_sep}{path_sep}"
        env["LUA_CPATH"] = f"{runtime_dir}{os.path.sep}?.{lua_ext}{path_sep}"

        print(f"Starting '{self.cmd} {' '.join(self.args)}' in '{self.cwd}'...", file=sys.stderr)
        
        self.proc = await asyncio.create_subprocess_exec(
            self.cmd,
            *self.args,
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Wait for the ready banner
        while True:
            try:
                line = await self.proc.stdout.readline()
                if not line:
                    stderr_output = await self.proc.stderr.read()
                    raise ConnectionError(f"Lua process exited before becoming ready. Stderr: {stderr_output.decode()}")
                
                line_str = line.decode().strip()
                if line_str.startswith('{'):
                    msg = json.loads(line_str)
                    if msg.get("ready") is True:
                        print("Lua Bridge is ready.", file=sys.stderr)
                        return
            except json.JSONDecodeError:
                # Ignore non-json lines (logs)
                print(f"[LUA LOG] {line_str}", file=sys.stderr)
                continue
            except Exception as e:
                await self.stop()
                raise e

    async def stop(self):
        """
        Stops the luajit subprocess.
        """
        if self.proc:
            try:
                self.proc.kill()
                await self.proc.wait()
            except ProcessLookupError:
                pass # Process already dead
            self.proc = None
            print("Lua Bridge stopped.", file=sys.stderr)

    async def _send(self, action: str, params: dict = None):
        """
        Sends a JSON command to the Lua process and waits for a JSON response.
        """
        if not self.proc:
            raise ConnectionError("Process not started.")

        command = {"action": action}
        if params:
            command["params"] = params

        request_str = json.dumps(command)
        self.proc.stdin.write(request_str.encode() + b'
')
        await self.proc.stdin.drain()

        # Read lines until a valid JSON response is found
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                 stderr_output = await self.proc.stderr.read()
                 raise ConnectionError(f"Lua process exited while waiting for response. Stderr: {stderr_output.decode()}")

            line_str = line.decode().strip()
            if line_str.startswith('{'):
                try:
                    response = json.loads(line_str)
                    if not response.get("ok"):
                        raise RuntimeError(f"Lua API Error: {response.get('error', 'Unknown error')}")
                    return response
                except json.JSONDecodeError:
                    print(f"[LUA LOG] {line_str}", file=sys.stderr)
                    continue
                except Exception as e:
                    await self.stop()
                    raise e
    
    # --- High-level API methods ---

    async def load_build_xml(self, xml: str, name: str = "API Build"):
        return await self._send("load_build_xml", {"xml": xml, "name": name})

    async def get_stats(self, fields: list = None):
        params = {"fields": fields} if fields else {}
        return await self._send("get_stats", params)

    async def get_build_info(self):
        return await self._send("get_build_info")


async def main():
    """
    Main function to handle command-line arguments and run the client.
    """
    parser = argparse.ArgumentParser(description="A command-line interface for the Path of Building Headless Lua Bridge.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Sub-parser for 'get-info' command
    info_parser = subparsers.add_parser("get-info", help="Load a build from an XML file and get its basic info.")
    info_parser.add_argument("xml_file", type=str, help="Path to the PoB build XML file.")

    # Sub-parser for 'get-stats' command
    stats_parser = subparsers.add_parser("get-stats", help="Load a build from XML and get specific stats.")
    stats_parser.add_argument("xml_file", type=str, help="Path to the PoB build XML file.")
    stats_parser.add_argument("--fields", nargs='+', help="A list of specific stat fields to retrieve (e.g., 'Life', 'TotalDPS').")

    args = parser.parse_args()

    if not POB_FORK_PATH.exists():
        print(f"Error: Path of Building 'src' directory not found at '{POB_FORK_PATH}'", file=sys.stderr)
        print("Please check the POB_FORK_PATH constant in this script.", file=sys.stderr)
        sys.exit(1)

    client = PoBCLI(cmd=POB_CMD, args=POB_ARGS, cwd=POB_FORK_PATH)
    
    try:
        await client.start()

        # Read the XML file content
        try:
            with open(args.xml_file, 'r', encoding='utf-8') as f:
                xml_content = f.read()
        except FileNotFoundError:
            print(f"Error: XML file not found at '{args.xml_file}'", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error reading XML file: {e}", file=sys.stderr)
            sys.exit(1)

        # Load the build first
        await client.load_build_xml(xml_content)
        print(f"Successfully loaded build from '{args.xml_file}'", file=sys.stderr)

        # Execute the requested command
        if args.command == "get-info":
            info = await client.get_build_info()
            print(json.dumps(info, indent=2))

        elif args.command == "get-stats":
            stats = await client.get_stats(fields=args.fields)
            print(json.dumps(stats, indent=2))

    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)
    finally:
        await client.stop()


if __name__ == "__main__":
    # For Windows, we might need to set a different event loop policy
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
