import { useCallback, useEffect, useState } from "react";
import { apiClient, type DatasetMetadata, type RunListItem } from "./api/client";
import { DataExplorerPage } from "./pages/DataExplorerPage";
import { SimulatorPage } from "./pages/SimulatorPage";
import { TaxLedgerPage } from "./pages/TaxLedgerPage";

type TabId = "simulator" | "datasets" | "ledger";

export function App() {
  const [activeTab, setActiveTab] = useState<TabId>("simulator");
  const [datasets, setDatasets] = useState<DatasetMetadata[]>([]);
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  const refreshDatasets = useCallback(async () => {
    const list = await apiClient.listDatasets();
    setDatasets(list);
  }, []);

  const refreshRuns = useCallback(async () => {
    const list = await apiClient.listRuns();
    setRuns(list);
  }, []);

  useEffect(() => {
    void refreshDatasets();
    void refreshRuns();
  }, [refreshDatasets, refreshRuns]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <h1>idx-workbench</h1>
          <span>Direct Indexing Simulator · Paired ETF Backtester · Tax-Lot Ledger</span>
        </div>
        <nav className="nav-tabs">
          <button
            type="button"
            className={`nav-tab ${activeTab === "simulator" ? "active" : ""}`}
            onClick={() => setActiveTab("simulator")}
          >
            Simulator & Sweeps
          </button>
          <button
            type="button"
            className={`nav-tab ${activeTab === "datasets" ? "active" : ""}`}
            onClick={() => setActiveTab("datasets")}
          >
            Data Explorer ({datasets.length})
          </button>
          <button
            type="button"
            className={`nav-tab ${activeTab === "ledger" ? "active" : ""}`}
            onClick={() => setActiveTab("ledger")}
          >
            Tax Ledger & Wash Sales
          </button>
        </nav>
      </header>

      <main className="main-content">
        {activeTab === "simulator" && (
          <SimulatorPage
            datasets={datasets}
            runs={runs}
            selectedRunId={selectedRunId}
            onSelectRunId={setSelectedRunId}
            onRefreshRuns={refreshRuns}
            onOpenLedgerForRun={(rid) => {
              setSelectedRunId(rid);
              setActiveTab("ledger");
            }}
          />
        )}
        {activeTab === "datasets" && (
          <DataExplorerPage
            datasets={datasets}
            onRefreshDatasets={refreshDatasets}
          />
        )}
        {activeTab === "ledger" && (
          <TaxLedgerPage
            runs={runs}
            selectedRunId={selectedRunId}
            onSelectRunId={setSelectedRunId}
          />
        )}
      </main>
    </div>
  );
}
