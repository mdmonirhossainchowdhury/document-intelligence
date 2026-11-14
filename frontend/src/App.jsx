import { useState } from "react";
import Upload from "./components/Upload.jsx";
import DocumentList from "./components/DocumentList.jsx";
import DocumentDetail from "./components/DocumentDetail.jsx";
import Search from "./components/Search.jsx";

const TABS = [
  { id: "upload", label: "Upload" },
  { id: "documents", label: "Documents" },
  { id: "search", label: "Search" },
];

export default function App() {
  const [tab, setTab] = useState("upload");
  const [selectedId, setSelectedId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const open = (id) => {
    setSelectedId(id);
    setTab("documents");
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">Document Intelligence</div>
        <nav className="tabs" aria-label="Main">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={tab === t.id ? "tab active" : "tab"}
              aria-current={tab === t.id ? "page" : undefined}
              onClick={() => {
                setTab(t.id);
                if (t.id !== "documents") setSelectedId(null);
              }}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="content">
        {tab === "upload" && <Upload onOpen={open} onUploaded={() => setRefreshKey((k) => k + 1)} />}
        {tab === "documents" && (selectedId ? (
          <DocumentDetail id={selectedId} onBack={() => setSelectedId(null)} onChanged={() => setRefreshKey((k) => k + 1)} />
        ) : (
          <DocumentList refreshKey={refreshKey} onOpen={open} />
        ))}
        {tab === "search" && <Search onOpen={open} />}
      </main>
    </div>
  );
}
