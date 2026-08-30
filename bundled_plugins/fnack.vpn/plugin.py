"""Bundled first-party plugin: VPN (WireGuard/OpenVPN).

Wraps services/vpn_service.py 1:1. The split-mode HTTP CONNECT proxy
(scripts/http_proxy.py) is part of this plugin's machinery — it is spawned
by vpn_service, not a separate library_task (audit finding, Phase 1).

The settings_tab slot renders the full VPN panel (upload .ovpn/.conf,
start/stop, live status incl. public IP + handshake) by calling the core
/api/vpn/* routes — the plugin owns the UI home for VPN config; the engine
stays in core (vpn_service reads /config/vpn/*.ovpn|*.conf).
"""

from plugins.base import VPNPlugin
from services.vpn_service import get_vpn_status, start_vpn, stop_vpn


class VPNPluginImpl(VPNPlugin):
    def on_load(self):
        self.context.ui.register_slot("settings_tab", self._render_settings_tab)

    def start(self) -> tuple[bool, str]:
        return start_vpn()

    def stop(self) -> tuple[bool, str]:
        return stop_vpn()

    def status(self) -> dict:
        return get_vpn_status()

    def _render_settings_tab(self, context_data: dict) -> str:
        return """
<div class="card bg-dark-card border-0 shadow-sm mb-4">
  <div class="card-body p-4">
    <div class="d-flex align-items-center justify-content-between mb-2">
      <h5 class="fw-bold m-0"><i class="fas fa-shield-halved text-danger me-2"></i>VPN (WireGuard / OpenVPN)</h5>
      <span class="badge bg-secondary-subtle text-secondary" id="vpnStatusBadge">Unknown</span>
    </div>
    <p class="text-secondary small mb-3">Split-mode tunnel: only download/metadata traffic goes through the VPN; the dashboard stays reachable.</p>

    <div class="bg-dark p-3 rounded mb-3">
      <div class="small text-secondary fw-bold mb-2">Upload a config</div>
      <div class="d-flex gap-2 flex-wrap">
        <input type="file" class="form-control form-control-sm" id="vpnConfigFileInput" accept=".ovpn,.conf" style="max-width: 340px;">
        <button class="btn btn-sm btn-brand" onclick="fnackVpnUpload()"><i class="fas fa-upload me-1"></i>Upload</button>
      </div>
      <div class="text-secondary small mt-2">OpenVPN (.ovpn) or WireGuard (.conf). The tunnel starts after upload if you press Start.</div>
    </div>

    <div class="small text-secondary fw-bold mb-2" id="vpnStatusText">Loading VPN status...</div>
    <div class="d-flex gap-2 flex-wrap">
      <button class="btn btn-sm btn-success" onclick="fnackVpnStart()"><i class="fas fa-play me-1"></i>Start</button>
      <button class="btn btn-sm btn-outline-danger" onclick="fnackVpnStop()"><i class="fas fa-stop me-1"></i>Stop</button>
    </div>
  </div>
</div>
<script>
(function() {
  async function refresh() {
    try {
      const r = await fetch('/api/vpn/status');
      const st = await r.json();
      const badge = document.getElementById('vpnStatusBadge');
      const statusEl = document.getElementById('vpnStatusText');
      if (!badge || !statusEl) return;
      badge.textContent = st.running ? 'Running' : 'Stopped';
      badge.className = 'badge ' + (st.running ? 'bg-success-subtle text-success' : 'bg-secondary-subtle text-secondary');
      let html = '';
      if (st.running && st.handshake_ok) {
        html = 'Connected via <strong>' + (st.type || 'VPN') + '</strong>' + (st.public_ip ? ' — public IP <strong>' + st.public_ip + '</strong>' : '');
      } else if (st.running) {
        html = 'Tunnel up but <strong>no recent handshake</strong> — downloads through it will fail yet.';
      } else if (st.config_files && st.config_files.length) {
        html = 'Config present (' + (st.config_files.join(', ')) + ') but VPN not running.';
      } else {
        html = 'No VPN config yet. Upload an OpenVPN (.ovpn) or WireGuard (.conf) file above.';
      }
      statusEl.innerHTML = html;
    } catch (e) {
      const statusEl = document.getElementById('vpnStatusText');
      if (statusEl) statusEl.textContent = 'Error loading VPN status.';
    }
  }
  window.fnackVpnStart = async function() {
    const r = await fetch('/api/vpn/start', { method: 'POST' });
    const d = await r.json();
    showToast(d.message || (d.success ? 'VPN started' : 'Start failed'), d.success ? 'success' : 'danger');
    refresh();
  };
  window.fnackVpnStop = async function() {
    const r = await fetch('/api/vpn/stop', { method: 'POST' });
    const d = await r.json();
    showToast(d.message || 'VPN stopped', d.success ? 'success' : 'danger');
    refresh();
  };
  window.fnackVpnUpload = async function() {
    const input = document.getElementById('vpnConfigFileInput');
    if (!input || !input.files || !input.files[0]) { showToast('Select a .ovpn or .conf file first', 'error'); return; }
    const fd = new FormData();
    fd.append('file', input.files[0]);
    const r = await fetch('/api/vpn/config', { method: 'POST', body: fd });
    const d = await r.json();
    showToast(d.message || d.error || 'Uploaded', r.ok ? 'success' : 'danger');
    if (r.ok) { input.value = ''; refresh(); }
  };
  document.addEventListener('DOMContentLoaded', refresh);
  if (document.readyState !== 'loading') refresh();
})();
</script>"""
