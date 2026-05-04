/**
 * Web-Host Dashboard - Frontend Application
 */

// ===== CONFIG =====
const DEFAULT_API_URL = "http://localhost:8000";
let API_URL = localStorage.getItem("webhost_api_url") || DEFAULT_API_URL;
let API_KEY = localStorage.getItem("webhost_api_key") || "";

// ===== NAVIGATION =====
function initNavigation() {
    document.querySelectorAll("[data-section]").forEach((link) => {
        link.addEventListener("click", (e) => {
            e.preventDefault();
            const section = link.dataset.section;
            showSection(section);
        });
    });

    // Handle hash navigation
    const hash = window.location.hash.replace("#", "");
    if (hash) showSection(hash);
}

function showSection(name) {
    document.querySelectorAll(".section").forEach((s) => s.classList.remove("active"));
    document.querySelectorAll(".nav-link").forEach((l) => l.classList.remove("active"));

    const section = document.getElementById(name);
    if (section) {
        section.classList.add("active");
        window.location.hash = name;
    }

    const navLink = document.querySelector(`.nav-link[data-section="${name}"]`);
    if (navLink) navLink.classList.add("active");

    if (name === "dashboard") loadDashboard();
    if (name === "sites") loadSites();
}

// ===== THEME =====
function initTheme() {
    const saved = localStorage.getItem("webhost_theme") || "dark";
    document.documentElement.setAttribute("data-theme", saved);
    updateThemeIcon(saved);

    document.getElementById("themeToggle").addEventListener("click", () => {
        const current = document.documentElement.getAttribute("data-theme");
        const next = current === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        localStorage.setItem("webhost_theme", next);
        updateThemeIcon(next);
    });
}

function updateThemeIcon(theme) {
    const icon = document.querySelector("#themeToggle i");
    icon.className = theme === "dark" ? "fas fa-sun" : "fas fa-moon";
}

// ===== API =====
async function apiFetch(endpoint, options = {}) {
    const url = `${API_URL}${endpoint}`;
    const headers = { ...options.headers };
    if (API_KEY) headers["X-API-Key"] = API_KEY;

    try {
        const res = await fetch(url, { ...options, headers });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.error || `HTTP ${res.status}`);
        }
        return await res.json();
    } catch (err) {
        if (err.name === "TypeError" && err.message.includes("fetch")) {
            throw new Error("Cannot connect to server. Check API URL in Settings.");
        }
        throw err;
    }
}

// ===== DASHBOARD =====
async function loadDashboard() {
    try {
        const stats = await apiFetch("/api/stats");
        document.getElementById("totalSites").textContent = stats.total_sites || 0;
        document.getElementById("totalVisits").textContent = stats.total_visits || 0;
        document.getElementById("totalUsers").textContent = stats.total_users || 0;
        document.getElementById("serverStatus").textContent = "Online";

        const sitesData = await apiFetch("/api/sites");
        renderRecentSites(sitesData.sites || []);
    } catch (err) {
        document.getElementById("serverStatus").textContent = "Offline";
        document.getElementById("recentSites").innerHTML = `
            <div class="empty-state">
                <i class="fas fa-exclamation-triangle"></i>
                <p>${err.message}</p>
            </div>`;
    }
}

function renderRecentSites(sites) {
    const container = document.getElementById("recentSites");

    if (!sites.length) {
        container.innerHTML = `
            <div class="empty-state">
                <i class="fas fa-inbox"></i>
                <p>No sites deployed yet</p>
                <a href="#upload" class="btn btn-primary btn-sm" data-section="upload">Upload your first site</a>
            </div>`;
        initNavigation();
        return;
    }

    const recent = sites.slice(-5).reverse();
    container.innerHTML = recent
        .map(
            (site) => `
        <div class="site-item">
            <div class="site-info">
                <div class="site-name">${escapeHtml(site.name)}</div>
                <a href="${escapeHtml(site.url)}" target="_blank" class="site-url">${escapeHtml(site.url)}</a>
                <div class="site-meta">${site.visits} visits &bull; ${site.files} files</div>
            </div>
            <div class="site-actions">
                <a href="${escapeHtml(site.url)}" target="_blank" class="btn btn-outline btn-sm">
                    <i class="fas fa-external-link-alt"></i> Visit
                </a>
            </div>
        </div>`
        )
        .join("");
}

// ===== SITES =====
async function loadSites() {
    const grid = document.getElementById("sitesGrid");
    grid.innerHTML = '<div class="empty-state"><div class="spinner"></div><p>Loading sites...</p></div>';

    try {
        const data = await apiFetch("/api/sites");
        renderSiteCards(data.sites || []);
    } catch (err) {
        grid.innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1;">
                <i class="fas fa-exclamation-triangle"></i>
                <p>${err.message}</p>
            </div>`;
    }
}

function renderSiteCards(sites) {
    const grid = document.getElementById("sitesGrid");

    if (!sites.length) {
        grid.innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1;">
                <i class="fas fa-inbox"></i>
                <p>No sites deployed yet</p>
            </div>`;
        return;
    }

    grid.innerHTML = sites
        .map(
            (site) => `
        <div class="site-card" data-name="${escapeHtml(site.name)}">
            <div class="site-card-header">
                <div class="site-card-icon"><i class="fas fa-globe"></i></div>
                <div class="site-card-title">${escapeHtml(site.name)}</div>
            </div>
            <a href="${escapeHtml(site.url)}" target="_blank" class="site-card-url">${escapeHtml(site.url)}</a>
            <div class="site-card-stats">
                <span><i class="fas fa-eye"></i> ${site.visits} visits</span>
                <span><i class="fas fa-file"></i> ${site.files} files</span>
                <span><i class="fas fa-database"></i> ${formatBytes(site.size_bytes)}</span>
            </div>
            <div class="site-card-actions">
                <a href="${escapeHtml(site.url)}" target="_blank" class="btn btn-primary btn-sm">
                    <i class="fas fa-external-link-alt"></i> Visit
                </a>
                <button class="btn btn-outline btn-sm" onclick="copyUrl('${escapeHtml(site.url)}')">
                    <i class="fas fa-copy"></i> Copy URL
                </button>
                <button class="btn btn-danger btn-sm" onclick="showDeleteModal('${escapeHtml(site.name)}')">
                    <i class="fas fa-trash"></i>
                </button>
            </div>
        </div>`
        )
        .join("");
}

// Search sites
function initSiteSearch() {
    const search = document.getElementById("searchSites");
    if (!search) return;

    search.addEventListener("input", () => {
        const query = search.value.toLowerCase();
        document.querySelectorAll(".site-card").forEach((card) => {
            const name = card.dataset.name.toLowerCase();
            card.style.display = name.includes(query) ? "" : "none";
        });
    });

    document.getElementById("refreshSites").addEventListener("click", loadSites);
}

// ===== UPLOAD =====
function initUpload() {
    const zone = document.getElementById("uploadZone");
    const fileInput = document.getElementById("fileInput");
    const form = document.getElementById("uploadForm");
    const siteNameInput = document.getElementById("siteName");
    let selectedFile = null;

    // Drag & Drop
    zone.addEventListener("click", () => fileInput.click());

    zone.addEventListener("dragover", (e) => {
        e.preventDefault();
        zone.classList.add("dragover");
    });

    zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));

    zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("dragover");
        const file = e.dataTransfer.files[0];
        if (file) handleFileSelect(file);
    });

    fileInput.addEventListener("change", () => {
        if (fileInput.files[0]) handleFileSelect(fileInput.files[0]);
    });

    // Remove file
    document.getElementById("removeFile").addEventListener("click", () => {
        selectedFile = null;
        fileInput.value = "";
        document.getElementById("fileInfo").style.display = "none";
        zone.style.display = "";
    });

    // Site name preview
    siteNameInput.addEventListener("input", () => {
        const name = siteNameInput.value.trim() || "site-name";
        document.getElementById("sitePreviewUrl").textContent = `${API_URL}/${name}/`;
    });

    // Form submit
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (!selectedFile) {
            showToast("Please select a ZIP file", "error");
            return;
        }
        await uploadSite(siteNameInput.value.trim(), selectedFile);
    });

    function handleFileSelect(file) {
        if (!file.name.endsWith(".zip")) {
            showToast("Only ZIP files are allowed", "error");
            return;
        }
        if (file.size > 50 * 1024 * 1024) {
            showToast("File too large! Max 50MB", "error");
            return;
        }
        selectedFile = file;
        document.getElementById("fileName").textContent = file.name;
        document.getElementById("fileSize").textContent = formatBytes(file.size);
        document.getElementById("fileInfo").style.display = "flex";
        zone.style.display = "none";
    }
}

async function uploadSite(name, file) {
    const deployBtn = document.getElementById("deployBtn");
    const progress = document.getElementById("uploadProgress");
    const result = document.getElementById("uploadResult");

    deployBtn.disabled = true;
    deployBtn.innerHTML = '<div class="spinner"></div> Deploying...';
    progress.style.display = "block";
    result.style.display = "none";

    const formData = new FormData();
    formData.append("file", file);
    formData.append("site_name", name);

    try {
        // Simulate progress
        const progressFill = document.getElementById("progressFill");
        const progressText = document.getElementById("progressText");
        progressFill.style.width = "30%";
        progressText.textContent = "Uploading...";

        const data = await apiFetch("/api/upload", {
            method: "POST",
            body: formData,
        });

        progressFill.style.width = "100%";
        progressText.textContent = "Deployed!";

        // Show result
        setTimeout(() => {
            progress.style.display = "none";
            result.style.display = "block";
            document.getElementById("resultLink").textContent = data.url;
            document.getElementById("resultLink").href = data.url;
            document.getElementById("visitSite").href = data.url;
            showToast("Site deployed successfully!", "success");
        }, 500);
    } catch (err) {
        showToast(err.message, "error");
        progress.style.display = "none";
    }

    deployBtn.disabled = false;
    deployBtn.innerHTML = '<i class="fas fa-rocket"></i> Deploy Website';
}

function resetUpload() {
    document.getElementById("uploadForm").reset();
    document.getElementById("uploadProgress").style.display = "none";
    document.getElementById("uploadResult").style.display = "none";
    document.getElementById("fileInfo").style.display = "none";
    document.getElementById("uploadZone").style.display = "";
    document.getElementById("sitePreviewUrl").textContent = `${API_URL}/site-name/`;
}

// ===== DELETE =====
let deleteSiteName = "";

function showDeleteModal(name) {
    deleteSiteName = name;
    document.getElementById("deleteSiteName").textContent = name;
    document.getElementById("deleteModal").classList.add("active");
}

function closeModal() {
    document.getElementById("deleteModal").classList.remove("active");
    deleteSiteName = "";
}

function initDeleteConfirm() {
    document.getElementById("confirmDelete").addEventListener("click", async () => {
        if (!deleteSiteName) return;

        try {
            await apiFetch(`/api/sites/${deleteSiteName}`, { method: "DELETE" });
            showToast(`Site '${deleteSiteName}' deleted`, "success");
            closeModal();
            loadSites();
            loadDashboard();
        } catch (err) {
            showToast(err.message, "error");
        }
    });

    // Close modal on backdrop click
    document.getElementById("deleteModal").addEventListener("click", (e) => {
        if (e.target === document.getElementById("deleteModal")) closeModal();
    });
}

// ===== SETTINGS =====
function initSettings() {
    const apiUrlInput = document.getElementById("apiUrl");
    const apiKeyInput = document.getElementById("apiKey");

    apiUrlInput.value = API_URL;
    apiKeyInput.value = API_KEY;

    document.getElementById("saveSettings").addEventListener("click", () => {
        API_URL = apiUrlInput.value.trim().replace(/\/$/, "") || DEFAULT_API_URL;
        API_KEY = apiKeyInput.value.trim();

        localStorage.setItem("webhost_api_url", API_URL);
        localStorage.setItem("webhost_api_key", API_KEY);

        document.getElementById("sitePreviewUrl").textContent = `${API_URL}/site-name/`;
        showToast("Settings saved!", "success");
        loadDashboard();
    });

    document.getElementById("toggleApiKey").addEventListener("click", () => {
        const input = apiKeyInput;
        const icon = document.querySelector("#toggleApiKey i");
        if (input.type === "password") {
            input.type = "text";
            icon.className = "fas fa-eye-slash";
        } else {
            input.type = "password";
            icon.className = "fas fa-eye";
        }
    });
}

// ===== UTILITIES =====
function showToast(message, type = "info") {
    const toast = document.getElementById("toast");
    toast.textContent = message;
    toast.className = `toast ${type} show`;
    setTimeout(() => (toast.className = "toast"), 3000);
}

function copyUrl(url) {
    navigator.clipboard
        .writeText(url)
        .then(() => showToast("URL copied!", "success"))
        .catch(() => showToast("Failed to copy", "error"));
}

function formatBytes(bytes) {
    if (!bytes) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// ===== INIT =====
document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initNavigation();
    initUpload();
    initSiteSearch();
    initDeleteConfirm();
    initSettings();
    loadDashboard();
});
