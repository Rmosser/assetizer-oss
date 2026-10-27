/** Content that opens and closes by its height (grid rows 0fr ↔ 1fr). It stays mounted so it can animate
 *  both ways; shut, it is inert and hidden. */
export function Fold({ open, children }: { open: boolean; children: React.ReactNode }) {
  return (
    <div className={`fold-y${open ? " open" : ""}`} inert={!open}>
      <div>{children}</div>
    </div>
  );
}
