const API_BASE = '/api';

async function api(method, path, body = null) {
    const opts = { method, headers: {} };
    if (body !== null) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(API_BASE + path, opts);
    if (!res.ok) {
        const text = await res.text();
        throw new Error(text || res.statusText);
    }
    return res.json();
}

async function loadConfig() {
    try {
        const cfg = await api('GET', '/config');
        document.getElementById('qbit-url').value = cfg.qbittorrent_url || '';
        document.getElementById('qbit-user').value = cfg.qbittorrent_user || '';
        document.getElementById('qbit-password').value = cfg.qbittorrent_password || '';
        document.getElementById('scan-interval').value = cfg.scan_interval || 60;
        document.getElementById('schedule-enabled').checked = cfg.schedule_enabled !== false;

        renderWindows(cfg.pause_windows || []);
    } catch (e) {
        console.error('Failed to load config:', e);
    }
}

async function loadStatus() {
    try {
        const status = await api('GET', '/status');
        const now = new Date();
        document.getElementById('current-time').textContent = now.toLocaleTimeString('en-GB');

        const stateEl = document.getElementById('schedule-state');
        if (!status.schedule_enabled) {
            stateEl.textContent = 'DISABLED';
            stateEl.className = 'status-value paused';
        } else if (status.is_paused) {
            stateEl.textContent = 'PAUSED (Window Active)';
            stateEl.className = 'status-value paused';
        } else {
            stateEl.textContent = 'RUNNING';
            stateEl.className = 'status-value active';
        }

        const windows = status.pause_windows || [];
        document.getElementById('active-windows').textContent =
            windows.length ? windows.join(', ') : 'None';

        document.getElementById('torrent-count').textContent = status.torrent_count ?? '--';
        document.getElementById('paused-count').textContent = status.paused_count ?? '--';

        const schedEl = document.getElementById('scheduler-status');
        schedEl.textContent = status.scheduler_running ? 'Running' : 'Stopped';
        schedEl.className = 'status-value ' + (status.scheduler_running ? 'running' : 'stopped');
    } catch (e) {
        console.error('Failed to load status:', e);
    }
}

function renderWindows(windows) {
    const list = document.getElementById('windows-list');
    list.innerHTML = '';
    windows.forEach((w, i) => {
        const row = document.createElement('div');
        row.className = 'window-row';
        row.innerHTML = `
            <input type="time" class="win-start" value="${w.start}" step="300">
            <span>to</span>
            <input type="time" class="win-end" value="${w.end}" step="300">
            <button class="btn-remove" onclick="removeWindow(this)">Remove</button>
        `;
        list.appendChild(row);
        row.querySelectorAll('input').forEach(inp => inp.addEventListener('change', saveSchedule));
    });
}

function addWindow() {
    const list = document.getElementById('windows-list');
    const row = document.createElement('div');
    row.className = 'window-row';
    row.innerHTML = `
        <input type="time" class="win-start" value="23:00" step="300">
        <span>to</span>
        <input type="time" class="win-end" value="07:00" step="300">
        <button class="btn-remove" onclick="removeWindow(this)">Remove</button>
    `;
    list.appendChild(row);
    row.querySelectorAll('input').forEach(inp => inp.addEventListener('change', saveSchedule));
    saveSchedule();
}

function removeWindow(btn) {
    btn.closest('.window-row').remove();
    saveSchedule();
}

function getWindows() {
    const rows = document.querySelectorAll('.window-row');
    const windows = [];
    rows.forEach(row => {
        const start = row.querySelector('.win-start').value;
        const end = row.querySelector('.win-end').value;
        if (start && end) {
            windows.push({ start, end });
        }
    });
    return windows;
}

async function saveConnection() {
    const el = document.getElementById('connection-status');
    el.textContent = 'Saving...';
    el.className = 'status-msg';
    try {
        const body = {
            qbittorrent_url: document.getElementById('qbit-url').value.trim(),
            qbittorrent_user: document.getElementById('qbit-user').value.trim(),
            qbittorrent_password: document.getElementById('qbit-password').value,
            scan_interval: parseInt(document.getElementById('scan-interval').value) || 60,
        };
        await api('PUT', '/config', body);
        el.textContent = 'Saved';
        el.className = 'status-msg ok';
        setTimeout(() => { el.textContent = ''; }, 3000);
    } catch (e) {
        el.textContent = 'Error: ' + e.message;
        el.className = 'status-msg error';
    }
}

let _saveTimer = null;
async function saveSchedule() {
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(async () => {
        const el = document.getElementById('schedule-status');
        el.textContent = 'Saving...';
        el.className = 'status-msg';
        try {
            const body = {
                pause_windows: getWindows(),
                schedule_enabled: document.getElementById('schedule-enabled').checked,
            };
            await api('PUT', '/config', body);
            el.textContent = 'Saved';
            el.className = 'status-msg ok';
            setTimeout(() => { el.textContent = ''; }, 3000);
            loadStatus();
        } catch (e) {
            el.textContent = 'Error: ' + e.message;
            el.className = 'status-msg error';
        }
    }, 500);
}

async function onToggleChange() {
    const el = document.getElementById('schedule-status');
    try {
        await api('PUT', '/config', {
            schedule_enabled: document.getElementById('schedule-enabled').checked,
        });
        loadStatus();
    } catch (e) {
        el.textContent = 'Error: ' + e.message;
        el.className = 'status-msg error';
    }
}

async function fetchLogs() {
    try {
        const data = await api('GET', '/logs');
        const el = document.getElementById('log-output');
        if (!data.logs || data.logs.length === 0) {
            el.innerHTML = '<span class="info">No logs yet.</span>';
            return;
        }
        el.innerHTML = data.logs.map(l => {
            const levelClass = l.level === 'WARNING' ? 'warn'
                : l.level === 'ERROR' ? 'error'
                : l.level === 'INFO' ? 'info' : '';
            return `<span class="time">${l.time}</span> <span class="${levelClass}">[${l.level}]</span> ${l.msg}<br>`;
        }).join('');
        el.scrollTop = el.scrollHeight;
    } catch (e) {
        console.error('Failed to fetch logs:', e);
    }
}

loadConfig();
loadStatus();
fetchLogs();
setInterval(loadStatus, 5000);
setInterval(fetchLogs, 10000);
