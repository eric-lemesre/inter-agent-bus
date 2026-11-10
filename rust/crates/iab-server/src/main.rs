//! Native MCP server of the inter-agent bus: the 24 tools of
//! `servers/shared_memory/server.py`, over stdio, on the same database
//! (`CONTRAT.md`, § 12). Every agent client spawns its own instance;
//! sharing happens through the database. No state logic here.

mod spec;
mod tools;

use std::process::ExitCode;
use std::sync::Arc;

use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, Implementation, ListToolsResult,
    PaginatedRequestParams, ServerCapabilities, ServerConfig, Tool,
};
use rmcp::service::RequestContext;
use rmcp::{ErrorData, RoleServer, ServerHandler, ServiceExt};
use serde_json::json;

use crate::spec::TOOLS;
use crate::tools::Client;

/// The server: its tool list, built once.
#[derive(Debug, Clone)]
struct BusServer {
    tools: Arc<Vec<Tool>>,
}

impl BusServer {
    fn new() -> Self {
        let tools = TOOLS
            .iter()
            .map(|spec| {
                Tool::new(spec.name, spec.description, tools::input_schema(spec))
                    .with_raw_output_schema(Arc::new(tools::output_schema(spec)))
            })
            .collect();
        Self {
            tools: Arc::new(tools),
        }
    }
}

/// A completed call: the text, also as the structured `{"result": …}`
/// the announced output schema promises.
fn answered(text: String) -> CallToolResult {
    let mut result = CallToolResult::success(vec![ContentBlock::text(text.clone())]);
    result.structured_content = Some(json!({ "result": text }));
    result
}

impl ServerHandler for BusServer {
    fn get_info(&self) -> ServerConfig {
        let config = ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("InterAgentBus", env!("CARGO_PKG_VERSION")));
        match tools::instructions() {
            Some(text) => config.with_instructions(text),
            None => config,
        }
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items(self.tools.as_ref().clone()))
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        self.tools.iter().find(|tool| tool.name == name).cloned()
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let client = context.peer.peer_info().map(|params| Client {
            name: params.client_info.name.clone(),
            title: params.client_info.title.clone(),
            version: params.client_info.version.clone(),
        });
        let name = request.name.into_owned();
        let arguments = request.arguments.unwrap_or_default();
        // The bus is synchronous and wait_signal blocks for minutes: every
        // call runs on the blocking pool, the transport keeps serving.
        let outcome = {
            let name = name.clone();
            tokio::task::spawn_blocking(move || tools::call(&name, &arguments, client.as_ref())).await
        };
        let result = match outcome {
            Ok(Ok(text)) => answered(text),
            Ok(Err(text)) => CallToolResult::error(vec![ContentBlock::text(text)]),
            Err(failure) => CallToolResult::error(vec![ContentBlock::text(format!(
                "Error executing tool {name}: {failure}"
            ))]),
        };
        Ok(result.into())
    }
}

async fn serve() -> Result<(), String> {
    let service = BusServer::new()
        .serve(rmcp::transport::stdio())
        .await
        .map_err(|error| format!("MCP handshake failed: {error}"))?;
    service
        .waiting()
        .await
        .map(drop)
        .map_err(|error| format!("MCP service stopped: {error}"))
}

fn main() -> ExitCode {
    if let Some(arg) = std::env::args().nth(1) {
        let text = match arg.as_str() {
            "-V" | "--version" => format!("iab-server {}", env!("CARGO_PKG_VERSION")),
            "-h" | "--help" => "iab-server — MCP server of the inter-agent bus, over stdio. Launched by an \
                                agent client; takes no argument."
                .to_owned(),
            other => {
                eprintln!("iab-server: unexpected argument '{other}' (try --help)");
                return ExitCode::from(2);
            }
        };
        println!("{text}");
        return ExitCode::SUCCESS;
    }
    // A launched client is a present client: seen as soon as the server
    // starts, before its first tool call.
    tools::touch();
    let runtime = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("iab-server: cannot start the runtime: {error}");
            return ExitCode::FAILURE;
        }
    };
    match runtime.block_on(serve()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("iab-server: {error}");
            ExitCode::FAILURE
        }
    }
}
