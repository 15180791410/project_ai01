/* ai工作台 - 轻量可视化组件
   全部使用原生 DOM / CSS / SVG，不依赖 CDN，断网答辩也能正常渲染。
   图表只消费调用方传入的真实业务数据，不在前端生成业务指标。 */
(function () {
    "use strict";

    var DEFAULT_COLORS = [
        "#2563eb", "#7c3aed", "#06b6d4", "#22c55e",
        "#f59e0b", "#ef4444", "#64748b", "#ec4899"
    ];
    var lineSequence = 0;

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function normalize(data, colors) {
        return (data || []).map(function (item, index) {
            var value = Number(item && item.value);
            return {
                // key 必须随图表数据一起保留。点击阶段分布图/柱状图时要用它回查
                // 原始业务行，例如 stage=已入学；只保留 label/value 会导致
                // “图上显示 14 份，点进去过滤 0 条”。
                key: item && item.key != null ? String(item.key) : String(item.label || "未分类"),
                label: String((item && item.label) || "未分类"),
                value: isFinite(value) ? Math.max(0, value) : 0,
                color: (item && item.color) || (colors || DEFAULT_COLORS)[index % (colors || DEFAULT_COLORS).length],
                hint: String((item && item.hint) || "")
            };
        });
    }

    function target(id) {
        return typeof id === "string" ? document.getElementById(id) : id;
    }

    function empty(label) {
        return '<div class="yj-chart-empty"><span>◌</span><p>' +
            escapeHtml(label || "暂无可视化数据") + "</p></div>";
    }

    function bindInteractive(el, rows, options) {
        if (!options || typeof options.onClick !== "function") return;
        var panel = el.closest(".chart-panel");
        if (panel) panel.setAttribute("data-drill", "true");
        el.querySelectorAll("[data-yj-index]").forEach(function (node) {
            const index = Number(node.getAttribute("data-yj-index"));
            const activate = function () { options.onClick(rows[index], index); };
            node.addEventListener("click", activate);
            node.addEventListener("keydown", function (event) {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    activate();
                }
            });
        });
    }

    function donut(id, data, options) {
        var el = target(id);
        if (!el) return;
        options = options || {};
        var rows = normalize(data, options.colors);
        var total = rows.reduce(function (sum, row) { return sum + row.value; }, 0);
        if (!rows.length || total <= 0) {
            el.innerHTML = empty(options.emptyText);
            return;
        }
        var cursor = 0;
        var segments = rows.map(function (row) {
            var start = cursor;
            cursor += row.value / total * 100;
            return row.color + " " + start.toFixed(2) + "% " + cursor.toFixed(2) + "%";
        }).join(",");
        var centerValue = options.centerValue != null ? options.centerValue : total;
        var centerLabel = options.centerLabel || "总计";
        var legend = rows.map(function (row) {
            var pct = total ? (row.value / total * 100).toFixed(1) : "0.0";
            var interactive = typeof options.onClick === "function"
                ? ' role="button" tabindex="0" data-yj-index="' + rows.indexOf(row) + '"'
                : "";
            return '<div class="donut-legend-row"' + interactive + ">" +
                '<i style="background:' + row.color + '"></i>' +
                '<span class="dl-label" title="' + escapeHtml(row.label) + '">' + escapeHtml(row.label) + '</span>' +
                '<b>' + row.value + '</b><em>' + pct + '%</em></div>';
        }).join("");
        el.innerHTML =
            '<div class="donut-layout">' +
                '<div class="donut-visual" role="img" aria-label="' + escapeHtml(options.ariaLabel || centerLabel) + '">' +
                    '<div class="donut-ring" style="background:conic-gradient(' + segments + ')"></div>' +
                    '<div class="donut-center"><b>' + escapeHtml(centerValue) + '</b><span>' + escapeHtml(centerLabel) + '</span></div>' +
                '</div>' +
                '<div class="donut-legend">' + legend + '</div>' +
            '</div>';
        bindInteractive(el, rows, options);
    }

    function bars(id, data, options) {
        var el = target(id);
        if (!el) return;
        options = options || {};
        var rows = normalize(data, options.colors);
        var max = Math.max.apply(null, [1].concat(rows.map(function (row) { return row.value; })));
        if (!rows.length || !rows.some(function (row) { return row.value > 0; })) {
            el.innerHTML = empty(options.emptyText);
            return;
        }
        el.innerHTML = '<div class="yj-bars">' + rows.map(function (row) {
            var width = Math.max(row.value > 0 ? 3 : 0, row.value / max * 100);
            var interactive = typeof options.onClick === "function"
                ? ' role="button" tabindex="0" data-yj-index="' + rows.indexOf(row) + '"'
                : "";
            return '<div class="yj-bar-row"' + interactive + ">" +
                '<div class="yj-bar-meta"><span title="' + escapeHtml(row.label) + '">' +
                    escapeHtml(row.label) + '</span><b>' + escapeHtml(row.value) +
                    escapeHtml(options.unit || "") + '</b></div>' +
                '<div class="yj-bar-track"><i style="width:' + width.toFixed(2) +
                    '%;background:linear-gradient(90deg,' + row.color + ',' + row.color + 'b8)"></i></div>' +
                (row.hint ? '<div class="yj-bar-hint">' + escapeHtml(row.hint) + '</div>' : '') +
            '</div>';
        }).join("") + "</div>";
        bindInteractive(el, rows, options);
    }

    function funnel(id, data, options) {
        var el = target(id);
        if (!el) return;
        options = options || {};
        var rows = normalize(data, options.colors);
        var max = Math.max.apply(null, [1].concat(rows.map(function (row) { return row.value; })));
        if (!rows.length || !rows.some(function (row) { return row.value > 0; })) {
            el.innerHTML = empty(options.emptyText);
            return;
        }
        el.innerHTML = '<div class="yj-funnel">' + rows.map(function (row, index) {
            var width = 96 - index * (44 / Math.max(rows.length, 1));
            var rate = max ? Math.round(row.value / max * 100) : 0;
            var interactive = typeof options.onClick === "function"
                ? ' role="button" tabindex="0" data-yj-index="' + rows.indexOf(row) + '"'
                : "";
            return '<div class="funnel-row"' + interactive + ">" +
                '<div class="funnel-label"><span>' + escapeHtml(row.label) + '</span><b>' +
                    row.value + '</b></div>' +
                '<div class="funnel-track"><div class="funnel-stage" style="width:' +
                    Math.max(32, width).toFixed(1) + '%;background:linear-gradient(90deg,' +
                    row.color + ',' + row.color + '99)"><span>' + rate + '%</span></div></div>' +
            '</div>';
        }).join("") + "</div>";
        bindInteractive(el, rows, options);
    }

    function line(id, data, options) {
        var el = target(id);
        if (!el) return;
        options = options || {};
        var points = (data || []).map(function (item, index) {
            var value = Number(item && item.value);
            return {
                label: String((item && item.label) || ("#" + (index + 1))),
                value: isFinite(value) ? value : 0
            };
        });
        if (!points.length) {
            el.innerHTML = empty(options.emptyText);
            return;
        }
        var width = 640, height = 230, left = 42, right = 18, top = 22, bottom = 42;
        var gradientId = "yjLineFill" + (++lineSequence);
        var graphW = width - left - right, graphH = height - top - bottom;
        var min = options.min != null ? Number(options.min) : 0;
        var max = options.max != null ? Number(options.max) : Math.max.apply(null, [1].concat(points.map(function (p) { return p.value; })));
        if (max <= min) max = min + 1;
        var coords = points.map(function (point, index) {
            var x = left + (points.length === 1 ? graphW / 2 : index / (points.length - 1) * graphW);
            var ratio = (point.value - min) / (max - min);
            ratio = Math.max(0, Math.min(1, ratio));
            return { x: x, y: top + graphH - ratio * graphH, label: point.label, value: point.value };
        });
        var path = coords.map(function (point, index) {
            return (index ? "L" : "M") + point.x.toFixed(1) + "," + point.y.toFixed(1);
        }).join(" ");
        var area = path + " L" + coords[coords.length - 1].x.toFixed(1) + "," +
            (top + graphH) + " L" + coords[0].x.toFixed(1) + "," + (top + graphH) + " Z";
        var grid = "";
        for (var g = 0; g <= 4; g++) {
            var gy = top + graphH * g / 4;
            var gv = max - (max - min) * g / 4;
            grid += '<line x1="' + left + '" y1="' + gy + '" x2="' + (width - right) +
                '" y2="' + gy + '" class="line-grid"/><text x="' + (left - 8) +
                '" y="' + (gy + 4) + '" text-anchor="end" class="line-axis">' +
                Math.round(gv) + '</text>';
        }
        var dots = coords.map(function (point, index) {
            var showLabel = points.length <= 6 || index === 0 || index === points.length - 1;
            var interactive = typeof options.onClick === "function"
                ? ' role="button" tabindex="0" data-yj-index="' + index + '"'
                : "";
            return '<circle cx="' + point.x + '" cy="' + point.y +
                '" r="4.5" class="line-dot"' + interactive + '><title>' + escapeHtml(point.label + "：" + point.value) +
                '</title></circle>' +
                (showLabel ? '<text x="' + point.x + '" y="' + (height - 14) +
                    '" text-anchor="middle" class="line-label">' +
                    escapeHtml(point.label.slice(0, 8)) + '</text>' : "");
        }).join("");
        el.innerHTML =
            '<svg class="yj-line-svg" viewBox="0 0 ' + width + " " + height +
            '" role="img" aria-label="' + escapeHtml(options.ariaLabel || "趋势图") + '">' +
                '<defs><linearGradient id="' + gradientId + '" x1="0" y1="0" x2="0" y2="1">' +
                    '<stop offset="0%" stop-color="' + (options.color || "#2563eb") + '" stop-opacity=".28"/>' +
                    '<stop offset="100%" stop-color="' + (options.color || "#2563eb") + '" stop-opacity="0"/>' +
                '</linearGradient></defs>' +
                grid +
                '<path d="' + area + '" fill="url(#' + gradientId + ')"/>' +
                '<path d="' + path + '" fill="none" stroke="' + (options.color || "#2563eb") +
                    '" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>' +
                dots +
            '</svg>';
        bindInteractive(el, points, options);
    }

    function tree(id, root, options) {
        var el = target(id);
        if (!el || !root) return;
        options = options || {};
        var children = root.children || [];
        el.innerHTML =
            '<div class="yj-tree ' + escapeHtml(options.compact ? "compact" : "") + '">' +
                '<div class="tree-root">' +
                    '<span class="tree-node-icon">' + escapeHtml(root.icon || "粤") + '</span>' +
                    '<div><b>' + escapeHtml(root.label || "业务中心") + '</b>' +
                        (root.hint ? '<small>' + escapeHtml(root.hint) + '</small>' : '') + '</div>' +
                '</div>' +
                '<div class="tree-trunk"></div>' +
                '<div class="tree-children">' + children.map(function (branch) {
                    var items = branch.children || [];
                    return '<div class="tree-branch">' +
                        '<div class="tree-branch-head"><i></i><b>' + escapeHtml(branch.label) + '</b>' +
                            (branch.value != null ? '<em>' + escapeHtml(branch.value) + '</em>' : '') + '</div>' +
                        '<div class="tree-leaf-list">' + items.map(function (leaf) {
                            return '<span class="tree-leaf">' + escapeHtml(leaf.label || leaf) + '</span>';
                        }).join("") + '</div>' +
                    '</div>';
                }).join("") + '</div>' +
            '</div>';
    }

    function progressList(id, data, options) {
        var el = target(id);
        if (!el) return;
        options = options || {};
        var rows = normalize(data, options.colors);
        if (!rows.length) {
            el.innerHTML = empty(options.emptyText);
            return;
        }
        el.innerHTML = '<div class="yj-progress-list">' + rows.map(function (row) {
            var max = options.max != null ? Number(options.max) : 100;
            var width = Math.max(0, Math.min(100, row.value / max * 100));
            return '<div class="yj-progress-row">' +
                '<div><span>' + escapeHtml(row.label) + '</span><b>' +
                    escapeHtml(row.value) + escapeHtml(options.unit || "%") + '</b></div>' +
                '<div class="yj-progress-track"><i style="width:' + width +
                    '%;background:' + row.color + '"></i></div>' +
            '</div>';
        }).join("") + "</div>";
    }

    window.YJCharts = {
        donut: donut,
        bars: bars,
        funnel: funnel,
        line: line,
        tree: tree,
        progressList: progressList
    };
})();
