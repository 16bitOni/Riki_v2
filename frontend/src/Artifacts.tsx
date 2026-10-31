import { Download, FileText, Globe, Printer } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Reply } from "./Activity";
import { Artifact, api } from "./api";
import { Sheet, Skeleton, toast } from "./ui";

/** A file Riki made, shown under the reply that made it. */
export function ArtifactCard({ a, onOpen }: { a: Artifact; onOpen: () => void }) {
  const md = a.kind === "md";
  return (
    <button className="artifact-card" onClick={onOpen}>
      {md ? <FileText size={22} /> : <Globe size={22} />}
      <span>
        <strong>{a.title}</strong>
        <small>{md ? "Markdown · open to read or download" : "Page · open to view or save as PDF"}</small>
      </span>
    </button>
  );
}

export function ArtifactView({ id, onClose }: { id: string; onClose: () => void }) {
  const [a, setA] = useState<Artifact | null>(null);
  const frame = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    api<Artifact>(`/artifacts/${id}`)
      .then(setA)
      .catch(() => {
        toast("That file couldn't be opened.", "error");
        onClose();
      });
  }, [id]);

  const download = () => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([a?.content ?? ""], { type: "text/markdown" }));
    link.download = `${a?.title.replace(/[^\w -]+/g, "").trim() || "file"}.md`;
    link.click();
    URL.revokeObjectURL(link.href);
  };

  return (
    <Sheet title={a?.title ?? "Loading"} onClose={onClose} wide>
      {!a ? (
        <Skeleton lines={6} />
      ) : (
        <>
          <div className="artifact-bar">
            {a.kind === "md" ? (
              <button onClick={download}>
                <Download size={16} /> Download .md
              </button>
            ) : (
              <button onClick={() => frame.current?.contentWindow?.postMessage("riki-print", "*")} title="Opens print: choose “Save as PDF”">
                <Printer size={16} /> Save as PDF
              </button>
            )}
          </div>
          {a.kind === "md" ? (
            <div className="artifact-md">
              <Reply text={a.content ?? ""} />
            </div>
          ) : (
            <iframe ref={frame} className="artifact-frame" title={a.title} sandbox="allow-scripts allow-modals" src={`/api/artifacts/${id}/view`} />
          )}
        </>
      )}
    </Sheet>
  );
}
