# Browse telemetry signals

The dashboard uses a descriptor-driven signal tree for sources opened through
the **Telemetry source** panel. Expand its hierarchy or search by display name,
stable ID, full path, unit, source or device. Search is case-insensitive literal
text, so brackets and punctuation do not act as a regular expression. Clear the
search field with its clear button.

Every leaf shows both the display name and stable ID. Two signals called
"Reading" remain separate and selectable. Selecting a leaf updates the plot and
raw table and displays its full ID, path, unit, type, source, device and
availability reason. The tree and metadata are read-only.

Availability comes from descriptor metadata and the latest original sample.
An explicitly unavailable descriptor takes precedence over sample availability.
A missing reading is marked unavailable and can still be selected for inspection;
it cannot be dragged. A signal with no readings says **No readings yet**, rather
than implying it has a zero value. Valid zero and false readings are available.
Unavailable signals remain visible in gray with their reason in the metadata
panel and tooltip.

Search does not silently select a different signal. If it hides the selected ID,
the summary explains that the selection is hidden; clearing the filter restores
it. Availability updates change existing model items in place. A changed catalog
preserves the selected ID if present and clears it when removed. Source retries
preserve the selected ID and search text when the signal still exists.

## Stable drag contract

The Qt item model supports copy-only signal drags with MIME type
`application/x-lbr-signal-ids+json`. Its UTF-8 JSON payload has this shape:

```json
{"version": 1, "signals": [{"signal_id": "lab.temp", "source_id": "Source-A"}]}
```

The payload contains identities, never display labels or row numbers. Group
nodes, duplicate selected columns and unavailable signals do not fabricate
entries. The source model and its filtering proxy produce the same identity
payload. Plot/drop-target integration is owned by #37.

## Verification

Run `uv run --locked pytest tests/ui/test_signal_browser.py`.
The tests cover searching all metadata fields, literal punctuation, duplicate
names, missing/removed descriptors, unavailable and unknown states, stable
selection, availability updates without model reset, a 3,000-signal nested
catalog, and drags through the filtering proxy. The real dashboard test selects
a filtered simulator signal and compares its plotted timestamps and values
against the session records. A deliberately broken search-role binding makes
the ordinary search test fail.

The legacy **File > Open** CSV table retains its compatibility column list.
Use the source picker to exercise the new descriptor hierarchy.
