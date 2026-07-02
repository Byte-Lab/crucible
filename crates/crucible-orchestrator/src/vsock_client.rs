use anyhow::{Context, Result};
use crucible_common::protocol::{GuestCommand, GuestResponse};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_vsock::{VsockAddr, VsockStream};

const VSOCK_PORT: u32 = 5000;

pub struct VsockClient {
    cid: u32,
    timeout: Duration,
}

impl VsockClient {
    pub fn new(cid: u32, timeout: Duration) -> Self {
        Self { cid, timeout }
    }

    pub async fn send_command(&self, cmd: GuestCommand) -> Result<GuestResponse> {
        let mut stream = tokio::time::timeout(
            self.timeout,
            VsockStream::connect(VsockAddr::new(self.cid, VSOCK_PORT)),
        )
        .await
        .with_context(|| format!("vsock connect to CID {} timed out", self.cid))?
        .with_context(|| format!("failed to connect to guest CID {}", self.cid))?;

        let cmd_json =
            serde_json::to_vec(&cmd).context("failed to serialize guest command")?;
        let framed = frame_message(&cmd_json);
        stream
            .write_all(&framed)
            .await
            .context("failed to send command")?;

        let mut len_buf = [0u8; 4];
        stream
            .read_exact(&mut len_buf)
            .await
            .context("failed to read response length")?;
        let resp_len = u32::from_be_bytes(len_buf) as usize;

        let mut resp_buf = vec![0u8; resp_len];
        stream
            .read_exact(&mut resp_buf)
            .await
            .context("failed to read response body")?;

        serde_json::from_slice(&resp_buf).context("failed to parse guest response")
    }

    pub async fn health_check(&self) -> Result<GuestResponse> {
        self.send_command(GuestCommand::HealthCheck).await
    }

    pub async fn launch_game(
        &self,
        app_id: u64,
        args: Vec<String>,
    ) -> Result<GuestResponse> {
        self.send_command(GuestCommand::LaunchGame { app_id, args })
            .await
    }

    pub async fn stop_game(&self) -> Result<GuestResponse> {
        self.send_command(GuestCommand::StopGame).await
    }

    pub async fn start_profiling(
        &self,
        config: serde_json::Value,
    ) -> Result<GuestResponse> {
        self.send_command(GuestCommand::StartProfiling { config })
            .await
    }

    pub async fn stop_profiling(&self) -> Result<GuestResponse> {
        self.send_command(GuestCommand::StopProfiling).await
    }

    pub async fn get_metrics(&self) -> Result<GuestResponse> {
        self.send_command(GuestCommand::GetMetrics).await
    }

    pub async fn fetch_file(&self, path: String) -> Result<GuestResponse> {
        self.send_command(GuestCommand::FetchFile { path }).await
    }

    pub async fn setup_cgroups(&self, groups: Vec<String>) -> Result<GuestResponse> {
        self.send_command(GuestCommand::SetupCgroups { groups })
            .await
    }

    /// Apply optimizer-proposed sysctl tunings in the guest (before the
    /// comparison run). `sysctls` maps dotted keys to values.
    pub async fn apply_sysctls(
        &self,
        sysctls: serde_json::Map<String, serde_json::Value>,
    ) -> Result<GuestResponse> {
        self.send_command(GuestCommand::ApplySysctls {
            config: serde_json::json!({ "sysctls": sysctls }),
        })
        .await
    }
}

pub fn frame_message(data: &[u8]) -> Vec<u8> {
    let len = data.len() as u32;
    let mut framed = Vec::with_capacity(4 + data.len());
    framed.extend_from_slice(&len.to_be_bytes());
    framed.extend_from_slice(data);
    framed
}

pub fn parse_frame(framed: &[u8]) -> Result<(u32, &[u8])> {
    if framed.len() < 4 {
        anyhow::bail!("frame too short: {} bytes", framed.len());
    }
    let len = u32::from_be_bytes([framed[0], framed[1], framed[2], framed[3]]);
    Ok((len, &framed[4..]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_message_roundtrip() {
        let data = b"hello world";
        let framed = frame_message(data);
        assert_eq!(framed.len(), 4 + data.len());
        let (length, payload) = parse_frame(&framed).unwrap();
        assert_eq!(length, data.len() as u32);
        assert_eq!(payload, data);
    }

    #[test]
    fn frame_empty_message() {
        let framed = frame_message(b"");
        assert_eq!(framed.len(), 4);
        let (length, payload) = parse_frame(&framed).unwrap();
        assert_eq!(length, 0);
        assert_eq!(payload, b"");
    }

    #[test]
    fn send_command_serializes_correctly() {
        let cmd = GuestCommand::HealthCheck;
        let json = serde_json::to_vec(&cmd).unwrap();
        let framed = frame_message(&json);
        let len_bytes = &framed[..4];
        let len =
            u32::from_be_bytes([len_bytes[0], len_bytes[1], len_bytes[2], len_bytes[3]]);
        assert_eq!(len as usize, json.len());
    }
}
