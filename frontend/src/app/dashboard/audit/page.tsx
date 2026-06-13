"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import {
  Box,
  Card,
  CardContent,
  Typography,
  Skeleton,
  Chip,
  TextField,
  MenuItem,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  TablePagination,
  IconButton,
  Tooltip,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import { listAuditLogs, type AuditLogEntry, type AuditLogListResponse } from "@/lib/api/client";

const REFRESH_INTERVAL_MS = 15_000;

function timeAgo(date: Date): string {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

const ACTOR_TYPES = ["", "user", "api_key", "system"];

function statusCodeColor(code: number | null): "success" | "warning" | "error" | "default" {
  if (code === null) return "default";
  if (code < 300) return "success";
  if (code < 500) return "warning";
  return "error";
}

export default function AuditLogPage() {
  const [data, setData] = useState<AuditLogListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(50);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Filters
  const [filterAction, setFilterAction] = useState("");
  const [filterActorType, setFilterActorType] = useState("");
  const [filterStatusCode, setFilterStatusCode] = useState("");

  const fetchData = useCallback(async () => {
    try {
      const result = await listAuditLogs({
        limit: rowsPerPage,
        offset: page * rowsPerPage,
        action: filterAction || undefined,
        actor_type: filterActorType || undefined,
        status_code: filterStatusCode ? Number(filterStatusCode) : undefined,
      });
      setData(result);
      setLastUpdated(new Date());
    } catch {
      // Silently fail
    } finally {
      setLoading(false);
    }
  }, [page, rowsPerPage, filterAction, filterActorType, filterStatusCode]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  useEffect(() => {
    intervalRef.current = setInterval(fetchData, REFRESH_INTERVAL_MS);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [fetchData]);

  const handleChangePage = (_: unknown, newPage: number) => {
    setPage(newPage);
  };

  const handleChangeRowsPerPage = (event: React.ChangeEvent<HTMLInputElement>) => {
    setRowsPerPage(parseInt(event.target.value, 10));
    setPage(0);
  };

  return (
    <Box>
      {/* ── Header ──────────────────────────────────────────────────────── */}
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", mb: 3 }}>
        <Box>
          <Typography variant="h4" sx={{ fontWeight: 600 }}>
            Audit Log
          </Typography>
          <Typography variant="subtitle1" color="text.secondary">
            Immutable record of all system actions
          </Typography>
        </Box>
        <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
          {lastUpdated && !loading && (
            <Typography variant="caption" color="text.secondary">
              {timeAgo(lastUpdated)}
            </Typography>
          )}
          <Tooltip title="Refresh">
            <IconButton size="small" onClick={fetchData}>
              <RefreshIcon />
            </IconButton>
          </Tooltip>
        </Box>
      </Box>

      {/* ── Filters ─────────────────────────────────────────────────────── */}
      <Card sx={{ mb: 3 }}>
        <CardContent sx={{ p: 2, "&:last-child": { pb: 2 } }}>
          <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap" }}>
            <TextField
              label="Action"
              size="small"
              value={filterAction}
              onChange={(e) => { setFilterAction(e.target.value); setPage(0); }}
              placeholder="e.g. session.create"
              sx={{ minWidth: 200 }}
            />
            <TextField
              select
              label="Actor Type"
              size="small"
              value={filterActorType}
              onChange={(e) => { setFilterActorType(e.target.value); setPage(0); }}
              sx={{ minWidth: 140 }}
            >
              {ACTOR_TYPES.map((t) => (
                <MenuItem key={t} value={t}>
                  {t || "All"}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              select
              label="Status"
              size="small"
              value={filterStatusCode}
              onChange={(e) => { setFilterStatusCode(e.target.value); setPage(0); }}
              sx={{ minWidth: 120 }}
            >
              <MenuItem value="">All</MenuItem>
              <MenuItem value="200">2xx Success</MenuItem>
              <MenuItem value="400">4xx Error</MenuItem>
              <MenuItem value="500">5xx Error</MenuItem>
            </TextField>
          </Box>
        </CardContent>
      </Card>

      {/* ── Table ────────────────────────────────────────────────────────── */}
      <Card>
        <CardContent sx={{ p: 0, "&:last-child": { pb: 0 } }}>
          {loading ? (
            <Skeleton variant="rectangular" width="100%" height={400} sx={{ borderRadius: 1 }} />
          ) : !data || data.items.length === 0 ? (
            <Box sx={{ p: 4, textAlign: "center" }}>
              <Typography variant="body1" color="text.secondary">
                No audit log entries found.
              </Typography>
            </Box>
          ) : (
            <>
              <TableContainer component={Paper} elevation={0}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell sx={{ fontWeight: 600, whiteSpace: "nowrap" }}>Time</TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>Action</TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>Actor</TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>Type</TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>Method</TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>Path</TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>IP</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {data.items.map((entry: AuditLogEntry) => (
                      <TableRow key={entry.id} hover>
                        <TableCell sx={{ whiteSpace: "nowrap" }}>
                          <Typography variant="caption">
                            {timeAgo(new Date(entry.created_at))}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}>
                            {entry.action}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}>
                            {(entry.actor_id ?? "—").slice(0, 12)}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          {entry.actor_type ? (
                            <Chip label={entry.actor_type} size="small" variant="outlined" />
                          ) : (
                            <Typography variant="caption" color="text.disabled">—</Typography>
                          )}
                        </TableCell>
                        <TableCell>
                          {entry.status_code ? (
                            <Chip
                              label={entry.status_code}
                              size="small"
                              color={statusCodeColor(entry.status_code)}
                              variant="outlined"
                            />
                          ) : (
                            <Typography variant="caption" color="text.disabled">—</Typography>
                          )}
                        </TableCell>
                        <TableCell>
                          <Typography variant="caption">{entry.method ?? "—"}</Typography>
                        </TableCell>
                        <TableCell>
                          <Typography
                            variant="caption"
                            sx={{
                              maxWidth: 250,
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                              display: "block",
                              fontFamily: "monospace",
                              fontSize: "0.75rem",
                            }}
                          >
                            {entry.path ?? "—"}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <Typography variant="caption" color="text.secondary">
                            {entry.ip_address ?? "—"}
                          </Typography>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
              <TablePagination
                component="div"
                count={data.total}
                page={page}
                onPageChange={handleChangePage}
                rowsPerPage={rowsPerPage}
                onRowsPerPageChange={handleChangeRowsPerPage}
                rowsPerPageOptions={[25, 50, 100, 500]}
              />
            </>
          )}
        </CardContent>
      </Card>
    </Box>
  );
}
