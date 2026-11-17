"""Expose only task-declared MCP servers through the official local bridge."""
import json
import taskbridge


def definition(name, description, properties):
    return {'type':'function','function':{'name':name,'description':description,
        'parameters':{'type':'object','properties':properties,'required':list(properties)}}}


TOOLS = [
    definition('task_mcp_tools', 'Discover task MCP tools. Filter by a query; use empty query for all.',
               {'server':{'type':'string'},'query':{'type':'string'}}),
    definition('task_mcp_call', 'Call a discovered tool on a task-declared MCP server.',
               {'server':{'type':'string'},'name':{'type':'string'},'arguments':{'type':'object'}}),
]


class TaskMcpTools:
    def __init__(self, session, readonly=False, client_factory=taskbridge.MCPClient):
        self.session, self.readonly, self.client_factory = session, readonly, client_factory
        self.clients, self.catalogs = {}, {}

    async def catalog(self, name):
        servers = [s for s in self.session.mcp_servers if s.get('name') == name]
        if len(servers) != 1:
            raise ValueError('Server is not declared by this task')
        if name not in self.clients:
            client = self.client_factory(self.session, servers[0]['url'])
            await client.initialize()
            self.clients[name] = client
            self.catalogs[name] = {t['name']:t for t in await client.list_tools()}
        return self.catalogs[name]

    async def run(self, fn):
        try:
            args = json.loads(fn['arguments'])
            catalog = await self.catalog(args['server'])
            if fn['name'] == 'task_mcp_tools':
                query = args['query'].casefold()
                data = [t for t in catalog.values() if query in (t['name']+' '+t.get('description','')).casefold()]
                return json.dumps(data, ensure_ascii=False)[:50000]
            if fn['name'] != 'task_mcp_call' or args['name'] not in catalog:
                raise ValueError('Unknown MCP tool')
            tool = catalog[args['name']]
            if self.readonly and tool.get('annotations',{}).get('readOnlyHint') is not True:
                raise ValueError('This role requires an explicitly read-only MCP tool')
            if not isinstance(args['arguments'], dict):
                raise ValueError('Tool arguments must be an object')
            data = await self.clients[args['server']].call_tool(args['name'],args['arguments'])
            return json.dumps(data, ensure_ascii=False)[:50000]
        except Exception:
            # Do not leak source text, tool parameters or provider errors to logs.
            return 'MCP request failed or is not permitted for this role.'
