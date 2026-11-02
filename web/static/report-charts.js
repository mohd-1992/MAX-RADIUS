/* Shared charts for the sales and consumption reports. Values stay unchanged. */
(() => {
    'use strict';
    const charts = [];
    const valid = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
    const values = list => list.map(value => valid(value) ? value : null);
    const font = {family: 'Tajawal, sans-serif', size: 11};
    const numeric = new Intl.NumberFormat('en-US', {maximumFractionDigits: 2});
    const palette = () => document.documentElement.classList.contains('theme-light')
        ? {text: '#475569', grid: '#e2e8f0'} : {text: '#cbd5e1', grid: '#263449'};

    function options(unit, horizontal = false) {
        const colors = palette();
        return {
            responsive: true, maintainAspectRatio: false, locale: 'en-US',
            indexAxis: horizontal ? 'y' : 'x',
            interaction: {mode: 'index', intersect: false},
            plugins: {
                legend: {position: 'top', align: 'end', rtl: true,
                    labels: {color: colors.text, font, usePointStyle: true, boxWidth: 10}},
                tooltip: {rtl: true, titleAlign: 'right', bodyAlign: 'right',
                    callbacks: {label: context => {
                        const value = horizontal ? context.parsed.x : context.parsed.y;
                        return context.dataset.label + ': ' + (valid(value) ? numeric.format(value) + (unit ? ' ' + unit : '') : 'غير متاح');
                    }}}
            },
            scales: {
                x: {offset: true, beginAtZero: horizontal, ...(horizontal ? {min: 0} : {}),
                    grid: {display: horizontal, color: colors.grid},
                    ticks: {color: colors.text, font, maxRotation: 35},
                    title: {display: horizontal && !!unit, text: unit, color: colors.text, font}},
                y: {offset: horizontal, beginAtZero: !horizontal, ...(!horizontal ? {min: 0} : {}),
                    grid: {display: !horizontal, color: colors.grid},
                    ticks: {color: colors.text, font},
                    title: {display: !horizontal && !!unit, text: unit, color: colors.text, font}}
            }
        };
    }

    function create(canvas, config) {
        if (!canvas || typeof Chart === 'undefined') return null;
        const chart = new Chart(canvas, config);
        charts.push(chart);
        return chart;
    }

    function sales(canvas, data, label) {
        const settings = options('');
        settings.scales.y.title = {...settings.scales.y.title, display: true, text: 'قيمة المبيعات'};
        return create(canvas, {type: 'bar',
            data: {labels: data.labels, datasets: [{label, data: values(data.sales),
                backgroundColor: '#10b981', borderRadius: 5, maxBarThickness: 44}]},
            options: settings});
    }

    function periodTraffic(canvas, data) {
        return create(canvas, {type: 'line',
            data: {labels: data.labels, datasets: [
                {label: 'التحميل', data: values(data.download), borderColor: '#10b981',
                    backgroundColor: 'rgba(16,185,129,0.16)'},
                {label: 'الرفع', data: values(data.upload), borderColor: '#38bdf8',
                    backgroundColor: 'rgba(56,189,248,0.12)'}
            ].map(series => ({...series, fill: true, borderWidth: 2.5,
                pointBackgroundColor: series.borderColor,
                tension: 0.4, cubicInterpolationMode: 'monotone', pointRadius: 3, pointHoverRadius: 6,
                spanGaps: false}))},
            options: options('GiB')});
    }

    function serverTraffic(canvas, rows) {
        const largest = Math.max(0, ...rows.flatMap(row => [row.download, row.upload]).filter(valid));
        const [unit, divisor] = [['TiB', 1024 ** 4], ['GiB', 1024 ** 3],
            ['MiB', 1024 ** 2], ['KiB', 1024], ['B', 1]].find(pair => largest >= pair[1]) || ['B', 1];
        if (canvas) canvas.parentElement.style.height = Math.max(280, rows.length * 56) + 'px';
        const settings = options(unit, true);
        settings.scales.y.ticks.callback = function (value) {
            const label = this.getLabelForValue(value);
            return label.length > 34 ? label.slice(0, 33) + '…' : label;
        };
        return create(canvas, {type: 'bar', data: {
            labels: rows.map(row => (row.nas_label ? row.nas_label + ' / ' : '') + row.server_label),
            datasets: [{label: 'التحميل', data: rows.map(row => valid(row.download) ? row.download / divisor : null), backgroundColor: '#10b981'},
                {label: 'الرفع', data: rows.map(row => valid(row.upload) ? row.upload / divisor : null), backgroundColor: '#38bdf8'}]
                .map(series => ({...series, maxBarThickness: 18, borderRadius: 4}))
        }, options: settings});
    }

    // All three charts follow the theme switch without reloading the report.
    new MutationObserver(() => {
        const colors = palette();
        charts.forEach(chart => {
            chart.options.plugins.legend.labels.color = colors.text;
            Object.values(chart.options.scales).forEach(axis => {
                axis.ticks.color = colors.text;
                axis.grid.color = colors.grid;
                axis.title.color = colors.text;
            });
            chart.update('none');
        });
    }).observe(document.documentElement, {attributes: true, attributeFilter: ['class']});
    window.MaxReportCharts = {sales, periodTraffic, serverTraffic};
})();
