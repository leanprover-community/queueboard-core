const diff_stat = DataTable.type('diff_stat', {
  detect: function (data) { return false; },
  order: {
    pre: function (data) {
      // Input has the form
      // <span style="color:green">42</span>/<span style="color:red">13</span>,
      // we extract the tuple (42, 13) and compute their sum 42+13.
      let parts = data.split('</span>/<span', 2);
      return Number(parts[0].slice(parts[0].search(">") + 1)) + Number(parts[1].slice(parts[1].search(">") + 1, -7));
    }
  },
});
const formatted_relativedelta = DataTable.type('formatted_relativedelta', {
  detect: function (data) { return data.startsWith('<div style="display:none">'); },
  order: {
    pre: function (data) {
      let main = (data.split('</div>', 2))[0].slice('<div style="display:none">'.length);
      // If there is no input data, main is the empty string.
      if (!main.includes('-')) {
          return -1;
      }
      const [days, seconds, ...rest] = main.split('-');
      return 100000 * Number(days) + Number(seconds);
    }
  }
});
// A PR assignee is sorted as a string; except that the string "nobody"
// (i.e., a PR is unassigned) is sorted last.
const assignee = DataTable.type('assignee', {
  order: {
    pre: function (data) { return (data == 'nobody') ? "zzzzzzzzzz" : data; }
  },
});
let getIdx;
let getAlias;
if (STANDARD) {
  // Return a table column index corresponding to a human-readable alias.
  // |aliasOrIdx| is supposed to be an integer or a string;
  // |show_approvals| is true iff this table contains a visible column of github approvals.
  getIdx = function(aliasOrIdx, show_approvals) {
    // Some tables show a column of all PR approvals right after the assignee.
    // In this case, later indices must be shifted by 1.
    let offset = show_approvals ? 1 : 0;
    switch (aliasOrIdx) {
      case "number":
        return 0;
      case "author":
        return 1;
      case "title":
        return 2;
      // idx = 3 means the PR description, which is a hidden field
      case "labels":
        return 4;
      case "diff":
        return 5;
      // idx = 6 is the list of modified files
      case "numberChangedFiles":
        return 7;
      case "numberComments":
        return 8;
      // idx = 9 means the handles of users who commented or reviewed this PR
      case "assignee":
        return 10;
      // The following column indices depend on whether a dashboard shows
      // the list of users who approved a PR.
      case "approvals":
        if (show_approvals) { return 11; }
        break;
      case "lastUpdate":
        return 11 + offset;
      case "lastStatusChange":
        return 12 + offset;
      case "totalTimeReview":
        return 13 + offset;
      default:
        return aliasOrIdx;
    };
  }
  // an "inverse" function to getIdx
  getAlias = function(idx, show_approvals) {
    // Some tables show a column of all PR approvals right after the assignee.
    // In this case, later indices must be shifted by 1.
    switch (idx) {
      case 0:
        return "number";
      case 1:
        return "author";
      case 2:
        return "title";
      // idx = 3 means the PR description, which is a hidden field
      case 4:
        return "labels";
      case 5:
        return "diff";
      // idx = 6 is the list of modified files
      case 7:
        return "numberChangedFiles";
      case 8:
        return "numberComments";
      // idx = 9 means the handles of users who commented or reviewed this PR
      case 10:
        return "assignee";
      // The following column indices depend on whether a dashboard shows
      // the list of users who approved a PR.
      case 11:
        if (show_approvals) { return "approvals"; } else { return "lastUpdate"; }
      case 12:
        if (show_approvals) { return "lastUpdate"; } else { return "lastStatusChange"; }
      case 13:
        if (show_approvals) { return "lastStatusChange"; } else { return "totalTimeReview"; }
      case 14:
        if (show_approvals) { return "totalTimeReview"; } else { return idx; }
      default:
        return idx;
    };
  }
} else {
  // Return a table column index corresponding to a human-readable alias.
  // |aliasOrIdx| is supposed to be an integer or a string;
  // |show_approvals| is true iff this table contains a visible column of github approvals.
  getIdx = function (aliasOrIdx) {
    switch (aliasOrIdx) {
      case "number":
        return 0;
      case "author":
        return 1;
      case "title":
        return 2;
      // case "fromFork":
      case "labels":
        return 3;
      case "ciStatus":
        return 4;
      case "isBlocked":
        return 5;
      case "hasMergeConflict":
        return 6;
      case "isReady":
        return 7;
      case "awaitingReview":
        return 8;
      case "missingTopicLabel":
        return 10;
      case "overallStatus":
        return 11;
      default:
        return aliasOrIdx;
    };
  }
  // an "inverse" function to getIdx
  getAlias = function (idx) {
    switch (idx) {
      case 0:
        return "number";
      case 1:
        return "author";
      case 2:
        return "title";
      case 3:
        // return "fromFork";
        return "labels";
      case 4:
        return "ciStatus";
      case 5:
        return "isBlocked";
      case 6:
        return "hasMergeConflict";
      case 7:
        return "isReady";
      case 8:
        return "awaitingReview";
      case 10:
        return "missingTopicLabel";
      case 11:
        return "overallStatus";
      default:
        return idx;
    };
  }
}
const DEFAULT_LENGTH = 10;
const DEFAULT_LENGTH_STR = String(DEFAULT_LENGTH);
const EMPTY_FILTER_TOKEN = "__none__";
const PR_TYPE_OPTIONS = [
  ["feat", "feat (feature)"],
  ["fix", "fix (bug fix)"],
  ["doc", "doc (documentation)"],
  ["style", "style (formatting, missing semicolons, ...)"],
  ["refactor", "refactor"],
  ["test", "test (when adding missing tests)"],
  ["chore", "chore (maintain)"],
  ["perf", "perf (performance improvement, optimization, ...)"],
  ["ci", "ci (for changes to github workflows, other automation)"],
  ["other", "none of the above"],
];
const tableFilterState = new WeakMap();
const tableFilterContexts = [];

function isTopicLabel(name) {
  if (!name) {
    return false;
  }
  const lowered = name.toLowerCase();
  return lowered.startsWith("t-") || lowered === "ci" || lowered === "imo" || lowered === "tech debt";
}

function sortCaseInsensitive(values) {
  return [...values].sort((left, right) => left.localeCompare(right, undefined, { sensitivity: "base" }));
}

function dedupeCaseInsensitive(values) {
  const seen = new Set();
  const out = [];
  for (const value of values) {
    const normalized = String(value || "").trim();
    if (normalized === "") {
      continue;
    }
    const key = normalized.toLowerCase();
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    out.push(normalized);
  }
  return out;
}

function canonicalizeCaseInsensitive(values, allowedValues) {
  const allowedByKey = new Map(allowedValues.map((value) => [String(value).toLowerCase(), value]));
  const out = [];
  const seen = new Set();
  for (const value of values) {
    const key = String(value || "").trim().toLowerCase();
    if (key === "" || seen.has(key) || !allowedByKey.has(key)) {
      continue;
    }
    seen.add(key);
    out.push(allowedByKey.get(key));
  }
  return out;
}

function extractLabelNames(cellHtml) {
  if (cellHtml === null || cellHtml === undefined) {
    return [];
  }
  const container = document.createElement("div");
  container.innerHTML = String(cellHtml);
  const labels = [...container.querySelectorAll("span.label")].map((node) => node.textContent.replace(/\s+/g, " ").trim());
  return dedupeCaseInsensitive(labels);
}

function extractPrTypeFromTitle(cellHtml) {
  const title = sanitizeCellText(cellHtml);
  const match = title.match(/^(feat|fix|doc|style|refactor|test|chore|perf|ci)(?:\([^)]*\))?(?:!?)?:/i);
  return match ? match[1].toLowerCase() : "other";
}

function getLabelsColumnHtml(data) {
  return STANDARD ? (data[4] || "") : (data[3] || "");
}

function getTitleColumnHtml(data) {
  return data[2] || "";
}

function updateFilterSummary(summaryNode, label, selectedCount, totalCount) {
  if (totalCount === 0) {
    summaryNode.textContent = `${label} (0)`;
    return;
  }
  if (selectedCount === totalCount) {
    summaryNode.textContent = `${label} (all)`;
    return;
  }
  summaryNode.textContent = `${label} (${selectedCount}/${totalCount})`;
}

function renderCheckboxFilter(container, options, selectedValues, inputName, onChange) {
  container.innerHTML = "";
  for (const [value, label] of options) {
    const wrapper = document.createElement("label");
    wrapper.className = "qb-filter-option";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.name = inputName;
    checkbox.value = value;
    checkbox.checked = selectedValues.has(value);
    checkbox.addEventListener("change", onChange);

    const text = document.createElement("span");
    text.textContent = label;

    wrapper.appendChild(checkbox);
    wrapper.appendChild(text);
    container.appendChild(wrapper);
  }
}

function createFilterDropdown(label) {
  const details = document.createElement("details");
  details.className = "qb-filter-dropdown";

  const summary = document.createElement("summary");
  summary.className = "qb-filter-summary";
  details.appendChild(summary);

  const options = document.createElement("div");
  options.className = "qb-filter-options";
  details.appendChild(options);

  updateFilterSummary(summary, label, 0, 0);
  return { details, summary, options, label };
}

function createFilterActions(onSelectAll, onClearAll) {
  const actions = document.createElement("div");
  actions.className = "qb-filter-actions";

  const selectAll = document.createElement("button");
  selectAll.type = "button";
  selectAll.className = "qb-filter-action";
  selectAll.textContent = "Select all";
  selectAll.addEventListener("click", onSelectAll);

  const clearAll = document.createElement("button");
  clearAll.type = "button";
  clearAll.className = "qb-filter-action";
  clearAll.textContent = "Clear all";
  clearAll.addEventListener("click", onClearAll);

  actions.appendChild(selectAll);
  actions.appendChild(clearAll);
  return actions;
}

function collectTopicLabelsFromTable(table) {
  const labels = [];
  table.rows().every(function () {
    labels.push(...extractLabelNames(this.data()[STANDARD ? 4 : 3]).filter(isTopicLabel));
  });
  return sortCaseInsensitive(dedupeCaseInsensitive(labels));
}

function parseFilterParams(params) {
  const prTypeParams = dedupeCaseInsensitive(params.getAll("pr_type").map((value) => value.toLowerCase()));
  const topicLabelParams = dedupeCaseInsensitive(params.getAll("topic_label"));
  return {
    prTypes: prTypeParams.includes(EMPTY_FILTER_TOKEN) ? [] : prTypeParams,
    topicLabels: topicLabelParams.includes(EMPTY_FILTER_TOKEN) ? [] : topicLabelParams,
    hasPrTypeParam: params.has("pr_type"),
    hasTopicLabelParam: params.has("topic_label"),
  };
}

function setCheckboxSelection(container, selectedValues) {
  for (const input of container.querySelectorAll('input[type="checkbox"]')) {
    input.checked = selectedValues.has(input.value);
  }
}

function drawAllFilteredTables() {
  for (const { table, ignoreNext } of tableFilterContexts) {
    ignoreNext.search++;
    table.draw();
  }
}

function updateFilterParamsInUrl() {
  const url = new URL(window.location.href);
  url.searchParams.delete("pr_type");
  url.searchParams.delete("topic_label");

  const firstState = tableFilterContexts[0];
  if (!firstState) {
    window.history.pushState({}, "", url.toString());
    return;
  }

  if (firstState.prTypes.size === 0) {
    url.searchParams.append("pr_type", EMPTY_FILTER_TOKEN);
  } else if (firstState.prTypes.size !== firstState.prTypeOptions.length) {
    for (const value of firstState.prTypeOptions) {
      if (firstState.prTypes.has(value)) {
        url.searchParams.append("pr_type", value);
      }
    }
  }

  if (firstState.topicLabelOptions.length === 0) {
    // No available topic-label options on this page; omit the param entirely.
  } else if (firstState.topicLabels.size === 0) {
    url.searchParams.append("topic_label", EMPTY_FILTER_TOKEN);
  } else if (firstState.topicLabels.size !== firstState.topicLabelOptions.length) {
    for (const value of firstState.topicLabelOptions) {
      if (firstState.topicLabels.has(value)) {
        url.searchParams.append("topic_label", value);
      }
    }
  }

  window.history.pushState({}, "", url.toString());
}

function applyGlobalFilterSelection({ prTypes, topicLabels, pushHistory = true }) {
  for (const state of tableFilterContexts) {
    state.prTypes = new Set(prTypes);
    state.topicLabels = new Set(topicLabels);
    setCheckboxSelection(state.prTypeFilter.options, state.prTypes);
    setCheckboxSelection(state.topicFilter.options, state.topicLabels);
    updateFilterSummary(state.prTypeFilter.summary, "PR type", state.prTypes.size, state.prTypeOptions.length);
    updateFilterSummary(state.topicFilter.summary, "Topic label", state.topicLabels.size, state.topicLabelOptions.length);
  }
  drawAllFilteredTables();
  if (pushHistory) {
    updateFilterParamsInUrl();
  }
}

function installTableFilters(table, tableElement, topicLabels, initialFilters, ignoreNext) {
  const container = table.table().container();
  const searchContainer = container.querySelector(".dt-search, .dataTables_filter");
  if (!searchContainer) {
    return;
  }

  const filterBar = document.createElement("div");
  filterBar.className = "qb-filter-bar";

  const prTypeFilter = createFilterDropdown("PR type");
  const topicFilter = createFilterDropdown("Topic label");
  filterBar.appendChild(prTypeFilter.details);
  filterBar.appendChild(topicFilter.details);
  const exportButton = searchContainer.querySelector(".qb-export-btn");
  if (exportButton) {
    searchContainer.insertBefore(filterBar, exportButton);
  } else {
    searchContainer.appendChild(filterBar);
  }

  const state = {
    table,
    tableElement,
    ignoreNext,
    prTypeOptions: PR_TYPE_OPTIONS.map(([value]) => value),
    prTypes: new Set(
      initialFilters.hasPrTypeParam
        ? canonicalizeCaseInsensitive(initialFilters.prTypes, PR_TYPE_OPTIONS.map(([value]) => value))
        : PR_TYPE_OPTIONS.map(([value]) => value)
    ),
    topicLabelOptions: [...topicLabels],
    topicLabels: new Set(
      initialFilters.hasTopicLabelParam ? canonicalizeCaseInsensitive(initialFilters.topicLabels, topicLabels) : topicLabels
    ),
    prTypeFilter,
    topicFilter,
  };
  tableFilterState.set(tableElement, state);
  tableFilterContexts.push(state);

  const syncPrTypeSelection = () => {
    applyGlobalFilterSelection({
      prTypes: [...prTypeFilter.options.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value),
      topicLabels: [...state.topicLabels],
    });
  };
  const syncTopicSelection = () => {
    applyGlobalFilterSelection({
      prTypes: [...state.prTypes],
      topicLabels: [...topicFilter.options.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value),
    });
  };

  const syncPrTypeSelectionFromValues = (values) => {
    applyGlobalFilterSelection({
      prTypes: values,
      topicLabels: [...state.topicLabels],
    });
  };
  const syncTopicSelectionFromValues = (values) => {
    applyGlobalFilterSelection({
      prTypes: [...state.prTypes],
      topicLabels: values,
    });
  };

  renderCheckboxFilter(prTypeFilter.options, PR_TYPE_OPTIONS, state.prTypes, `${tableElement.id}-pr-type`, syncPrTypeSelection);
  prTypeFilter.options.prepend(
    createFilterActions(
      () => syncPrTypeSelectionFromValues([...state.prTypeOptions]),
      () => syncPrTypeSelectionFromValues([])
    )
  );
  updateFilterSummary(prTypeFilter.summary, "PR type", state.prTypes.size, state.prTypeOptions.length);

  const topicOptions = state.topicLabelOptions.map((value) => [value, value]);
  renderCheckboxFilter(topicFilter.options, topicOptions, state.topicLabels, `${tableElement.id}-topic-label`, syncTopicSelection);
  topicFilter.options.prepend(
    createFilterActions(
      () => syncTopicSelectionFromValues([...state.topicLabelOptions]),
      () => syncTopicSelectionFromValues([])
    )
  );
  updateFilterSummary(topicFilter.summary, "Topic label", state.topicLabels.size, state.topicLabelOptions.length);
}

$.fn.dataTable.ext.search.push(function (settings, data, dataIndex) {
  const tableElement = settings.nTable;
  const state = tableFilterState.get(tableElement);
  if (!state) {
    return true;
  }
  const rowData = new $.fn.dataTable.Api(settings).row(dataIndex).data() || data;

  const prType = extractPrTypeFromTitle(getTitleColumnHtml(rowData));
  if (state.prTypes.size !== state.prTypeOptions.length && !state.prTypes.has(prType)) {
    return false;
  }

  if (state.topicLabels.size !== state.topicLabelOptions.length) {
    if (state.topicLabels.size === 0) {
      return false;
    }
    const rowTopicLabels = extractLabelNames(getLabelsColumnHtml(rowData)).filter(isTopicLabel);
    if (!rowTopicLabels.some((label) => state.topicLabels.has(label))) {
      return false;
    }
  }

  return true;
});

function sanitizeCellText(cellHtml) {
  if (cellHtml === null || cellHtml === undefined) {
    return "";
  }
  if (typeof cellHtml !== "string") {
    return String(cellHtml);
  }
  const container = document.createElement("div");
  container.innerHTML = cellHtml;
  container.querySelectorAll('[style*="display:none"], [style*="display: none"], [hidden]').forEach((el) => el.remove());
  return container.textContent.replace(/\s+/g, " ").trim();
}

function makeExportFilename(tableId) {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const ts = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const page = (window.location.pathname.split("/").pop() || "dashboard").replace(/\.html$/, "");
  return `${page}-${tableId || "table"}-${ts}.json`;
}

function triggerJsonDownload(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

function exportTableToJson(table, tableElement) {
  const tableId = (tableElement && tableElement.id) ? tableElement.id : "table";
  const showApproval = tableId == "t-queue-stale-unassigned" || tableId == "t-queue-stale-assigned" || tableId == "t-approved";
  const visibleColumnIndexes = table.columns(':visible').indexes().toArray();
  const columns = visibleColumnIndexes.map((idx) => sanitizeCellText(table.column(idx).header().innerHTML));
  const rowIndexes = table.rows({ search: "applied", order: "applied" }).indexes().toArray();
  const rows = rowIndexes.map((rowIdx) => {
    const out = {};
    for (let i = 0; i < visibleColumnIndexes.length; i++) {
      const columnIdx = visibleColumnIndexes[i];
      const key = columns[i] || `col_${i}`;
      const value = table.cell(rowIdx, columnIdx).render('display');
      out[key] = sanitizeCellText(value);
    }
    return out;
  });
  const order = table.order().map(([idx, dir]) => {
    if (STANDARD) {
      return { idx, alias: getAlias(idx, showApproval), dir };
    }
    return { idx, alias: getAlias(idx), dir };
  });
  const payload = {
    metadata: {
      page_url: window.location.href,
      table_id: tableId,
      exported_at: new Date().toISOString(),
      search: table.search(),
      order,
      visible_columns: columns,
      row_count: rows.length,
    },
    rows,
  };
  triggerJsonDownload(payload, makeExportFilename(tableId));
}

function addExportButton(table, tableElement) {
  const container = table.table().container();
  const searchContainer = container.querySelector(".dt-search, .dataTables_filter");
  if (!searchContainer || searchContainer.querySelector(".qb-export-btn")) {
    return;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "qb-export-btn";
  button.textContent = "Download JSON";
  button.addEventListener("click", () => exportTableToJson(table, tableElement));
  searchContainer.appendChild(button);
}

function debounce(callback, delay = 500) {
  let timeout; // This variable is part of the closure
  return function(...args) { // The debounced function
    clearTimeout(timeout); // Clear any previous timeout
    timeout = setTimeout(() => {
      callback(...args); // Execute the original callback
    }, delay);
  };
}
function updateSearchParams(search) {
  const url = new URL(window.location.href);
  console.log('search', search);
  if (search === "") {
    url.searchParams.delete('search');
  } else {
    url.searchParams.set('search', search);
  }
  window.history.pushState({}, '', url.toString());
}
const debouncedUpdateSearchParams = debounce(updateSearchParams);
function optionsFromParams() {
  // Parse the URL for any initial configuration settings.
  // Future: use this for deciding which table to apply the options to.
  // let fragment = window.location.hash;
  const params = new URLSearchParams(document.location.search);
  const search_params = params.get("search");
  const pageLengthRaw = params.get("length");
  const pageLength = pageLengthRaw === null ? DEFAULT_LENGTH : Number(pageLengthRaw);
  const filterParams = parseFilterParams(params);
  const sort_params = params.getAll("sort");
  // The configuration for initial sorting of tables, for tables with and without approvals.
  let sort_config = [];
  let sort_config_approvals = [];
  for (const config of sort_params) {
    if (!config.includes('-')) {
      console.log(`invalid value ${config} passed as sort parameter`);
      continue;
    }
    const [col, dir, ...rest] = config.split('-');
    if (dir != "asc" && dir != "desc") {
      console.log(`invalid sorting direction ${dir} passed as sorting configuration`);
      continue;
    }
    // push to sort_config in same order as in sort_params
    if (STANDARD) {
      const idx = getIdx(col, false);
      const idxApprovals = getIdx(col, true);
      if (!Number.isInteger(Number(idx)) || !Number.isInteger(Number(idxApprovals))) {
        console.log(`invalid sorting column ${col} passed as sorting configuration`);
        continue;
      }
      sort_config.push([idx, dir]);
      sort_config_approvals.push([idxApprovals, dir]);
    } else {
      const idx = getIdx(col);
      if (!Number.isInteger(Number(idx))) {
        console.log(`invalid sorting column ${col} passed as sorting configuration`);
        continue;
      }
      sort_config.push([idx, dir]);
    }
   }
  if (STANDARD && sort_config.length === 0) {
    sort_config = [[getIdx("totalTimeReview", false), "desc"]];
    sort_config_approvals = [[getIdx("totalTimeReview", true), "desc"]];
  }
  const options = {
    stateDuration: 0,
    pageLength: pageLength,
    "searching": true,
    order: sort_config,
  };
  if (STANDARD) {
    options.columnDefs = [{ type: 'diff_stat', targets: 5 }, { type: 'assignee', targets: 10 }, { visible: false, targets: [3, 6, 9] } ];
  } else {
    // originally commented out by omit_column_config
    // NB. This is brittle; keep in sync with other changes!
    // options.columnDefs = [{ type: 'diff_stat', targets: 5 }, { visible: false, targets: [3, 6, 9] } ];
  }
  if (params.has("search")) {
    options.search = {
        search: search_params
    };
  }
  return ({ options, sort_config_approvals, sort_config, params, filterParams });
}

$(document).ready(function () {
  const {options, sort_config_approvals, sort_config, filterParams} = optionsFromParams();
  const tables = [];
  const pageTopicLabels = [];
  $('table').each(function () {
    let table;
    const tableElement = this;
    if (STANDARD) {
      const tableId = $(this).attr('id');
      let tableOptions = { ...options }
      const show_approval = tableId == "t-queue-stale-unassigned" || tableId == "t-queue-stale-assigned" || tableId == "t-approved";
      tableOptions.order = show_approval ? sort_config_approvals : sort_config;
      table = $(this).DataTable(tableOptions);
    } else {
      const tableId = $(this).attr('id') || "";
      if (tableId.startsWith("t-")) {
        table = $(this).DataTable(options);
      }
    }

    // an object that tracks the number of times to disable the event handlers below during updates from popstate events
    const ignoreNext = { search: 0, length: 0, order: 0 };
    tables.push({table, tableElement, ignoreNext});
    if (table) {
      addExportButton(table, tableElement);
      pageTopicLabels.push(...collectTopicLabelsFromTable(table));
    }

    // event handlers to update params when table settings are changed
    $(this).on('search.dt', function (e, settings) {
      if (ignoreNext.search === 0) {
        debouncedUpdateSearchParams(settings.api.search());
      } else {
        ignoreNext.search--;
      }
    });
    $(this).on('length.dt', function (e, settings) {
      if (ignoreNext.length === 0) {
        const len = settings.api.page.len();
        const url = new URL(window.location.href);
        const paramsLength = url.searchParams.get("length") || DEFAULT_LENGTH_STR;
        console.log('length', len);
        if (String(len) !== paramsLength) {
          if (len === DEFAULT_LENGTH) {
            url.searchParams.delete('length');
          } else {
            url.searchParams.set('length', len);
          }
          window.history.pushState({}, '', url.toString());
        }
      } else {
        ignoreNext.length--;
      }
    });
    $(this).on('order.dt', function (e, settings) {
      if (ignoreNext.order === 0) {
        const order = settings.api.order();
        console.log('order', order);
        const url = new URL(window.location.href);
        // reset sort params in current URL
        url.searchParams.delete('sort');
        for (const [idx, dir] of order) {
          let alias;
          if (STANDARD) {
            const tableId = $(this).attr('id');
            const show_approval = tableId == "t-queue-stale-unassigned" || tableId == "t-queue-stale-assigned" || tableId == "t-approved";
            alias = getAlias(idx, show_approval);
          } else {
            alias = getAlias(idx);
          }
          if (dir !== '') {
            url.searchParams.append('sort', `${alias}-${dir}`);
          }
        }
        window.history.pushState({}, '', url.toString());
      } else {
        ignoreNext.order--;
      }
    });
  });

  const sharedTopicLabels = sortCaseInsensitive(dedupeCaseInsensitive(pageTopicLabels));
  for (const {table, tableElement, ignoreNext} of tables) {
    if (table) {
      installTableFilters(table, tableElement, sharedTopicLabels, filterParams, ignoreNext);
    }
  }

  document.addEventListener("click", (event) => {
    for (const dropdown of document.querySelectorAll(".qb-filter-dropdown[open]")) {
      if (!dropdown.contains(event.target)) {
        dropdown.removeAttribute("open");
      }
    }
  });

  // handle query parameter changes when user clicks backwards / forwards in history
  // (popstate is not fired on pushState)
  $(window).on('popstate', function (event) {
    const {options, sort_config_approvals, sort_config, params, filterParams} = optionsFromParams();
    console.log('popstate', params, options, sort_config_approvals, sort_config, filterParams);
    // for each table, update search, length, and order settings
    for (const {table, tableElement, ignoreNext} of tables) {
      if (!table) {
        continue;
      }
      if (params.has("search") && table.search() !== options.search.search) {
        ignoreNext.search++;
        table.search(options.search.search);
      } else if (!params.has("search") && table.search() !== '') {
        ignoreNext.search++;
        table.search('');
      }

      if (table.page.len() !== options.pageLength) {
        ignoreNext.length++;
        table.page.len(options.pageLength);
      }

      if (STANDARD) {
        const tableId = tableElement.id || "";
        const show_approval = tableId == "t-queue-stale-unassigned" || tableId == "t-queue-stale-assigned" || tableId == "t-approved";
        ignoreNext.order++;
        ignoreNext.search++; // the draw() triggers a search
        table.order(show_approval ? sort_config_approvals : sort_config).draw();
      } else {
        ignoreNext.order++;
        ignoreNext.search++; // the draw() triggers a search
        table.order(sort_config).draw();
      }
    }
    applyGlobalFilterSelection({
      prTypes: filterParams.hasPrTypeParam
        ? canonicalizeCaseInsensitive(filterParams.prTypes, PR_TYPE_OPTIONS.map(([value]) => value))
        : PR_TYPE_OPTIONS.map(([value]) => value),
      topicLabels: filterParams.hasTopicLabelParam
        ? canonicalizeCaseInsensitive(
            filterParams.topicLabels,
            tableFilterContexts[0] ? tableFilterContexts[0].topicLabelOptions : []
          )
        : (tableFilterContexts[0] ? tableFilterContexts[0].topicLabelOptions : []),
      pushHistory: false,
    });
  });
});
