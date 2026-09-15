
import os
import asyncio
from typing import List

from google import genai
from google.genai import types
from google.genai.types import FunctionDeclaration, GenerateContentConfig, Type, Schema, Tool

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Narzędzia zdefiniowane w Pythonie, nie w MCP
from poe_tools import dispatch_tool


def json_schema_to_gemini_schema(schema: dict) -> Schema:
    if not schema:
        return Schema(type=Type.OBJECT)
    
    t_str = schema.get("type", "object").upper()
    gemini_type = getattr(Type, t_str, Type.OBJECT)
    if t_str == "NUMBER": gemini_type = Type.NUMBER
    elif t_str == "INTEGER": gemini_type = Type.INTEGER
    elif t_str == "STRING": gemini_type = Type.STRING
    elif t_str == "BOOLEAN": gemini_type = Type.BOOLEAN
    elif t_str == "ARRAY": gemini_type = Type.ARRAY
    elif t_str == "OBJECT": gemini_type = Type.OBJECT
    
    properties = {}
    if "properties" in schema:
        for k, v in schema["properties"].items():
            properties[k] = json_schema_to_gemini_schema(v)
            
    items = None
    if "items" in schema:
        items = json_schema_to_gemini_schema(schema["items"])
        
    return Schema(
        type=gemini_type,
        description=schema.get("description", ""),
        properties=properties if properties else None,
        items=items,
        required=schema.get("required", None)
    )

class PoBAgent:
    def __init__(self, server_script_path: str, project_id: str, location: str):
        self.server_script_path = server_script_path
        self.session = None
        self.client_ctx = None
        self.stdio_context = None
        
        self.ai_client = genai.Client(
            vertexai=True,
            project=project_id,
            location=location
        )
        self.model_name = "gemini-3-flash-preview"
        self.pob_tools_cache = None
        self.python_tools_cache = None

    async def connect(self):
        server_params = StdioServerParameters(
            command="node",
            args=[self.server_script_path]
        )
        self.stdio_context = stdio_client(server_params)
        read, write = await self.stdio_context.__aenter__()
        self.client_ctx = ClientSession(read, write)
        self.session = await self.client_ctx.__aenter__()
        await self.session.initialize()
        print("✅ Połączono z serwerem PoB MCP (Node.js)")

    async def disconnect(self):
        if self.client_ctx:
            await self.client_ctx.__aexit__(None, None, None)
        if self.stdio_context:
            await self.stdio_context.__aexit__(None, None, None)
        print("❌ Rozłączono z PoB MCP")

    async def get_tools_for_gemini(self, external_tools: List[FunctionDeclaration] = None) -> List[Tool]:
        if not self.pob_tools_cache:
            print("Pobieram definicje narzędzi z MCP...")
            response = await self.session.list_tools()
            tools_list = []
            for mcp_tool in response.tools:
                schema_dict = mcp_tool.inputSchema
                gemini_schema = json_schema_to_gemini_schema(schema_dict)
                func_decl = FunctionDeclaration(
                    name=mcp_tool.name.replace("-", "_"),
                    description=mcp_tool.description or "Brak opisu",
                    parameters=gemini_schema
                )
                tools_list.append(func_decl)
            self.pob_tools_cache = tools_list
            print(f"Pobrano {len(tools_list)} narzędzi MCP.")

        all_declarations = self.pob_tools_cache[:]
        if external_tools:
            self.python_tools_cache = {t.name for t in external_tools}
            all_declarations.extend(external_tools)
        
        return [Tool(function_declarations=all_declarations)]

    async def execute_tool(self, name: str, arguments: dict):
        # Sprawdź czy to narzędzie z Pythona
        if self.python_tools_cache and name in self.python_tools_cache:
            print(f"🐍 Uruchamiam narzędzie Pythona: {name} z argumentami: {arguments}")
            # dispatch_tool jest importowany z poe_tools.py
            return await dispatch_tool(name, arguments)
            
        # W przeciwnym wypadku to narzędzie MCP
        original_name = name.replace("_", "-")
        if name.startswith("lua_") or name.startswith("search_") or name.startswith("update_") or name.startswith("add_") or name.startswith("set_"):
            original_name = name
            
        print(f"🔧 Uruchamiam narzędzie MCP: {original_name} z argumentami: {arguments}")
        result = await self.session.call_tool(original_name, arguments)
        texts = [content.text for content in result.content if content.type == "text"]
        return "\n".join(texts)

    async def ask(self, prompt: str, external_tools: List[FunctionDeclaration] = None) -> str:
        print(f"👤 Pytanie do agenta: {prompt[:200]}...")
        
        gemini_tools = await self.get_tools_for_gemini(external_tools)

        system_instruction = "Jesteś ekspertem Path of Exile. Masz do dyspozycji narzędzia do interakcji z Path of Building (lua_*, add_*, set_*, itp.) oraz narzędzia do sprawdzania cen i wiki. Przeanalizuj pytanie, załaduj build do PoB (`lua_new_build_from_xml`), a następnie użyj narzędzi, aby odpowiedzieć na pytanie. Bądź konkretny i zwięzły."
        
        chat = self.ai_client.chats.create(
            model=self.model_name,
            config=GenerateContentConfig(
                tools=gemini_tools,
                temperature=0.1,
                system_instruction=system_instruction
            )
        )
        
        print("🧠 Pytam Gemini...")
        response = chat.send_message(prompt)
        
        turns = 0
        max_turns = 10
        
        while response.function_calls and turns < max_turns:
            turns += 1
            print(f"🔄 Tura narzędziowa {turns}...")
            
            tool_responses = []
            
            for call in response.function_calls:
                tool_name = call.name
                tool_args = {k: v for k, v in call.args.items()} if call.args else {}
                
                try:
                    result_text = await self.execute_tool(tool_name, tool_args)
                    tool_responses.append(
                        types.Part.from_function_response(
                            name=tool_name,
                            response={"result": result_text}
                        )
                    )
                except Exception as e:
                    print(f"❌ Błąd wykonania {tool_name}: {e}")
                    tool_responses.append(
                        types.Part.from_function_response(
                            name=tool_name,
                            response={"error": str(e)}
                        )
                    )
            
            print("🧠 Wysyłam wyniki do Gemini...")
            response = chat.send_message(tool_responses)
            
        return response.text
