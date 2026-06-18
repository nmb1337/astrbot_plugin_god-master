/* ============================================================
   AI智能表情包 - 管理面板 JavaScript
   通过 window.AstrBotPluginPage bridge 与后端通信
   ============================================================ */

const bridge = window.AstrBotPluginPage;

// ---- 全局状态 ----
let categoriesData = [];
let currentPreviewCategory = null;

// ---- DOM 引用 ----
function $(id) {
    return document.getElementById(id);
}

// ---- 工具函数 ----
function showMsg(elementId, text, type) {
    const el = $(elementId);
    if (!el) return;
    el.textContent = text;
    el.className = "save-msg " + (type || "");
    if (type === "success" || type === "error") {
        setTimeout(() => {
            el.textContent = "";
            el.className = "save-msg";
        }, 3000);
    }
}

// ---- API 调用封装 ----
async function apiGet(endpoint, params = {}) {
    try {
        return await bridge.apiGet(endpoint, params);
    } catch (e) {
        console.error("API GET error:", endpoint, e);
        throw e;
    }
}

async function apiPost(endpoint, body = {}) {
    try {
        return await bridge.apiPost(endpoint, body);
    } catch (e) {
        console.error("API POST error:", endpoint, e);
        throw e;
    }
}

// ---- 页面初始化 ----
async function init() {
    const context = await bridge.ready();
    console.log("[AI表情包] Bridge ready:", context);

    // 绑定事件
    $("btnRefreshStatus").addEventListener("click", loadStatus);
    $("btnSaveCategories").addEventListener("click", saveCategories);
    $("btnRescan").addEventListener("click", rescanImages);
    $("btnSavePrompt").addEventListener("click", savePrompt);

    // 初始加载
    await Promise.all([loadStatus(), loadCategories(), loadPrompt()]);
}

// ---- 加载插件状态 ----
async function loadStatus() {
    try {
        const data = await apiGet("status");
        $("statusEnabled").textContent = data.enabled ? "✅ 已启用" : "⛔ 已禁用";
        $("statusEnabled").style.color = data.enabled ? "var(--success)" : "var(--danger)";
        $("statusProbability").textContent = data.trigger_probability + "%";
        $("statusCooldown").textContent = data.cooldown_seconds + " 秒";
        $("statusCounts").textContent = data.total_categories + " 分类 / " + data.total_images + " 张图片";
    } catch (e) {
        console.error("加载状态失败:", e);
    }
}

// ---- 加载分类列表 ----
async function loadCategories() {
    try {
        const data = await apiGet("categories");
        categoriesData = data.categories || [];
        renderCategoryList();
        renderPreviewTabs();
    } catch (e) {
        console.error("加载分类失败:", e);
        $("categoryList").innerHTML = '<p class="loading" style="color:var(--danger)">加载失败，请检查插件状态</p>';
    }
}

function renderCategoryList() {
    const container = $("categoryList");
    if (categoriesData.length === 0) {
        container.innerHTML = '<p class="loading">暂无分类，请在 images 目录下添加分类文件夹并放入图片后重载插件。</p>';
        return;
    }

    container.innerHTML = categoriesData
        .map(
            (cat) => `
        <div class="category-item">
            <div class="category-header">
                <span class="category-name">📌 ${escapeHtml(cat.name)}</span>
                <span class="category-count">${cat.image_count} 张图片</span>
            </div>
            <input
                type="text"
                class="category-desc-input"
                data-category="${escapeHtml(cat.name)}"
                value="${escapeHtml(cat.description || '')}"
                placeholder="请输入分类描述，帮助 AI 理解该分类的用途"
            />
        </div>
    `
        )
        .join("");
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// ---- 保存分类描述 ----
async function saveCategories() {
    const inputs = document.querySelectorAll(".category-desc-input");
    const descriptions = {};
    inputs.forEach((input) => {
        const cat = input.dataset.category;
        descriptions[cat] = input.value.trim();
    });

    try {
        await apiPost("categories/save", { descriptions });
        showMsg("categorySaveMsg", "✅ 分类描述已保存", "success");
    } catch (e) {
        showMsg("categorySaveMsg", "❌ 保存失败: " + e.message, "error");
    }
}

// ---- 重新扫描 ----
async function rescanImages() {
    try {
        const data = await apiPost("rescan");
        showMsg("categorySaveMsg", `✅ 已重新扫描！共 ${data.total_categories} 个分类，${data.total_images} 张图片`, "success");
        await loadCategories();
        await loadStatus();
    } catch (e) {
        showMsg("categorySaveMsg", "❌ 扫描失败: " + e.message, "error");
    }
}

// ---- 图片预览标签 ----
function renderPreviewTabs() {
    const container = $("previewTabs");
    if (categoriesData.length === 0) {
        container.innerHTML = '<p class="loading">暂无分类</p>';
        return;
    }

    container.innerHTML = categoriesData
        .map(
            (cat) =>
                `<span class="preview-tab" data-category="${escapeHtml(cat.name)}">${escapeHtml(cat.name)} (${cat.image_count})</span>`
        )
        .join("");

    // 绑定点击事件
    container.querySelectorAll(".preview-tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            const cat = tab.dataset.category;
            // 切换 active 样式
            container.querySelectorAll(".preview-tab").forEach((t) => t.classList.remove("active"));
            tab.classList.add("active");
            // 加载预览
            loadPreview(cat);
        });
    });

    // 默认选中第一个分类
    if (categoriesData.length > 0) {
        const firstTab = container.querySelector(".preview-tab");
        if (firstTab) {
            firstTab.click();
        }
    }
}

async function loadPreview(category) {
    const container = $("previewContent");
    currentPreviewCategory = category;

    try {
        const data = await apiGet("images/" + encodeURIComponent(category));
        const images = data.images || [];

        if (images.length === 0) {
            container.innerHTML = '<p class="loading">该分类下暂无图片</p>';
            return;
        }

        // 构建图片预览 URL（通过后端 API 获取）
        container.innerHTML = images
            .map((imgName) => {
                const previewUrl = `image/preview/${encodeURIComponent(category)}/${encodeURIComponent(imgName)}`;
                return `
                <div class="preview-img-item">
                    <div class="preview-img-wrapper">
                        <img src="" data-src="${previewUrl}" alt="${escapeHtml(imgName)}" loading="lazy" />
                    </div>
                    <div class="preview-img-name">${escapeHtml(imgName)}</div>
                </div>
            `;
            })
            .join("");

        // 延迟加载图片（通过 bridge API 获取真实 URL）
        loadPreviewImages();
    } catch (e) {
        console.error("加载图片预览失败:", e);
        container.innerHTML = '<p class="loading" style="color:var(--danger)">加载失败</p>';
    }
}

async function loadPreviewImages() {
    const imgElements = document.querySelectorAll(".preview-img-wrapper img[data-src]");
    for (const img of imgElements) {
        const endpoint = img.dataset.src;
        if (!endpoint) continue;
        try {
            // 使用 apiGet 获取图片的二进制数据
            // 对于图片预览，我们直接使用 img src 指向 bridge 端点
            // bridge 会自动处理认证
            const result = await bridge.apiGet(endpoint);
            // 如果返回的是 blob URL 或其他格式，这里处理
            // 由于 bridge API 返回 JSON，图片预览可能需要用不同的方式
            // 这里使用直接请求的方式
            img.src = await getImageUrl(endpoint);
        } catch (e) {
            console.error("加载图片失败:", endpoint, e);
            img.alt = "加载失败";
        }
    }
}

async function getImageUrl(endpoint) {
    // 通过 bridge 下载图片并创建 object URL
    try {
        const result = await bridge.download(endpoint, {}, null);
        // download 返回 { filename: "..." }
        // 但我们需要实际的图片数据，这里尝试另一种方式
        // 直接用 fetch 访问 bridge 构建的完整 URL
        // 由于 bridge 的限制，我们使用 apiGet 方式
        const baseUrl = window.location.origin;
        const pluginName = "astrbot_plugin_ai_sticker";
        // 构造完整的 API URL
        const fullUrl = `${baseUrl}/api/v1/plugins/extensions/${pluginName}/${endpoint}`;
        return fullUrl;
    } catch (e) {
        console.error("获取图片 URL 失败:", e);
        return "";
    }
}

// ---- AI 提示词模板 ----
async function loadPrompt() {
    try {
        const data = await apiGet("prompt");
        $("promptTemplate").value = data.prompt_template || "";
    } catch (e) {
        console.error("加载提示词失败:", e);
    }
}

async function savePrompt() {
    const promptTemplate = $("promptTemplate").value;
    try {
        await apiPost("prompt/save", { prompt_template: promptTemplate });
        showMsg("promptSaveMsg", "✅ 提示词已保存", "success");
    } catch (e) {
        showMsg("promptSaveMsg", "❌ 保存失败: " + e.message, "error");
    }
}

// ---- 启动 ----
document.addEventListener("DOMContentLoaded", init);
