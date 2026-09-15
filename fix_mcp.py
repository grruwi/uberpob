with open("mcp_agent.py", "w") as f:
    f.write("""import asyncio
import os
import json
from typing import List
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from google import genai
from google.genai import types
from google.genai.types import Tool, FunctionDeclaration, GenerateContentConfig, Type, Schema

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
    def __init__(self, server_script_path: str):
        self.server_script_path = server_script_path
        self.session = None
        self.client_ctx = None
        self.stdio_context = None
        
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        
        self.ai_client = genai.Client(
            vertexai=True,
            project=project_id,
            location=location
        )
        self.model_name = "gemini-2.5-flash" 

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
        if self.client_ctx: await self.client_ctx.__aexit__(None, None, None)
        if self.stdio_context: await self.stdio_context.__aexit__(None, None, None)
        print("❌ Rozłączono z PoB MCP")

    async def get_tools_for_gemini(self) -> List[Tool]:
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
            
        print(f"Pobrano {len(tools_list)} narzędzi (z pełnymi schematami).")
        return [Tool(function_declarations=tools_list)]

    async def execute_tool(self, name: str, arguments: dict):
        original_name = name.replace("_", "-")
        if name.startswith("lua_") or name.startswith("search_") or name.startswith("update_") or name.startswith("add_") or name.startswith("set_"):
            original_name = name
        print(f"🔧 Uruchamiam narzędzie: {original_name} z argumentami: {arguments}")
        try:
            result = await self.session.call_tool(original_name, arguments)
            texts = [c.text for c in result.content if c.type == "text"]
            return "\n".join(texts)
        except Exception as e:
            print(f"❌ Błąd w module: {str(e)}")
            return f"Błąd wewnątrz narzędzia: {str(e)}"

    async def ask(self, prompt: str) -> str:
        print(f"\n👤 Pytanie: {prompt}")
        gemini_tools = await self.get_tools_for_gemini()

        system_instruction = "Jesteś asystentem PoE. Używaj narzędzi (lua_new_build, add_item, search_tree_nodes, update_tree_delta, lua_get_stats) aby obliczać interakcje dla gracza."
        
        chat = self.ai_client.chats.create(
            model=self.model_name,
            config=GenerateContentConfig(
                tools=gemini_tools,
                temperature=0.0,
                system_instruction=system_instruction
            )
        )
        
        print("🧠 Pytam Gemini...")
        response = chat.send_message(prompt)
        
        turns = 0
        max_turns = 10
        
        while response.function_calls and turns < max_turns:
            turns += 1
            print(f"\n🔄 Tura narzędziowa {turns}...")
            tool_responses = []
            
            for call in response.function_calls:
                tool_name = call.name
                tool_args = {}
                if call.args:
                    for k, v in call.args.items():
                        tool_args[k] = v
                
                result_text = await self.execute_tool(tool_name, tool_args)
                tool_responses.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response={"result": result_text}
                    )
                )
            
            print("🧠 Odsyłam wyniki do Gemini...")
            response = chat.send_message(tool_responses)
            
        return response.text

async def main():
    agent = PoBAgent(server_script_path="pob-mcp/pob-mcp/build/index.js")
    try:
        await agent.connect()
        question = "Mam postać z 10000 armora i keystone Transcendence. Dostaję phys hit za 10000. Ile stracę życia?"
        answer = await agent.ask(question)
        print(f"\n✅ Ostateczna Odpowiedź:\n{answer}")
    finally:
        await agent.disconnect()

if __name__ == "__main__":
    asyncio.run(main())""")
