"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import {
  Box,
  Button,
  Card,
  Typography,
  ToggleButtonGroup,
  ToggleButton,
  CircularProgress,
  Tooltip,
  Chip,
} from "@mui/material";
import { DataGrid, type GridColDef } from "@mui/x-data-grid";
import TableChartIcon from "@mui/icons-material/TableChart";
import ChatIcon from "@mui/icons-material/Chat";
import { getSessionMessages, ApiError } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

type MessageResponse = components["schemas"]["MessageResponse"];

interface MessageRow {
  id: string;
  role: string;
  content: string;
  token_count: number;
  sequence_number: number;
  created_at: string;
  metadata: Record<string, unknown>;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatTime(raw: string): string {
  try {
    return new Date(raw).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

function formatDate(raw: string): string {
  try {
    return new Date(raw).toLocaleString();
  } catch {
    return raw;
  }
}

// ─── Role colors for chat bubbles ─────────────────────────────────────────────

const ROLE_COLORS: Record<string, { bg: string; align: "flex-start" | "flex-end" | "center"; label: string }> = {
  user: {
    bg: "#1565C0",
    align: "flex-end",
    label: "User",
  },
  assistant: {
    bg: "#E3F2FD",
    align: "flex-start",
    label: "Assistant",
  },
  system: {
    bg: "#F5F5F5",
    align: "center",
    label: "System",
  },
  tool: {
    bg: "#FFF3E0",
    align: "center",
    label: "Tool",
  },
};

function getRoleStyle(role: string) {
  return ROLE_COLORS[role] ?? { bg: "#F5F5F5", align: "flex-start", label: role };
}

// ─── Component Props ──────────────────────────────────────────────────────────

interface MessagesTabProps {
  projectId: string;
  userId: string;
  sessionId: string;
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function MessagesTab({ projectId, userId, sessionId }: MessagesTabProps) {
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [viewMode, setViewMode] = useState<"chat" | "table">("chat");
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const chatEndRef = useRef<HTMLDivElement>(null);

  // Scroll to bottom on first load
  useEffect(() => {
    if (!loading && messages.length > 0 && !cursor) {
      chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [loading, cursor, messages.length]);

  // ── Fetch messages ──────────────────────────────────────────────────────────

  const fetchMessages = useCallback(async () => {
    setLoading(true);
    try {
      const result = await getSessionMessages(projectId, userId, sessionId, {
        limit: 100,
      });
      setMessages((result.data as MessageRow[]).reverse());
      setCursor(result.next_cursor ?? null);
      setHasMore(result.has_more);
    } catch (err) {
      console.error("Failed to load messages", err);
    } finally {
      setLoading(false);
    }
  }, [projectId, userId, sessionId]);

  const loadOlder = useCallback(async () => {
    if (loadingMore || !cursor) return;
    setLoadingMore(true);
    try {
      const result = await getSessionMessages(projectId, userId, sessionId, {
        limit: 100,
        cursor,
      });
      const newMessages = (result.data as MessageRow[]).reverse();
      setMessages((prev) => [...newMessages, ...prev]);
      setCursor(result.next_cursor ?? null);
      setHasMore(result.has_more);
    } catch (err) {
      console.error("Failed to load older messages", err);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, cursor, projectId, userId, sessionId]);

  useEffect(() => {
    fetchMessages();
  }, [fetchMessages]);

  // ── Table columns ───────────────────────────────────────────────────────────

  const columns: GridColDef<MessageRow>[] = [
    {
      field: "sequence_number",
      headerName: "#",
      width: 60,
      align: "center",
    },
    {
      field: "role",
      headerName: "Role",
      width: 100,
      renderCell: ({ value }) => (
        <Chip
          label={value}
          size="small"
          color={value === "user" ? "primary" : value === "assistant" ? "success" : "default"}
          variant="outlined"
        />
      ),
    },
    {
      field: "content",
      headerName: "Content",
      flex: 1,
      minWidth: 300,
      renderCell: ({ row }) => {
        const isExpanded = expandedRows.has(row.id);
        const text = isExpanded ? row.content : row.content.length > 300 ? row.content.slice(0, 300) + "..." : row.content;
        return (
          <Box
            onClick={() => {
              if (row.content.length > 300) {
                setExpandedRows((prev) => {
                  const next = new Set(prev);
                  if (next.has(row.id)) next.delete(row.id);
                  else next.add(row.id);
                  return next;
                });
              }
            }}
            sx={{
              cursor: row.content.length > 300 ? "pointer" : "default",
              whiteSpace: "pre-wrap",
              fontFamily: "monospace",
              fontSize: "0.8rem",
              lineHeight: 1.4,
              py: 0.5,
            }}
          >
            {text}
          </Box>
        );
      },
    },
    {
      field: "token_count",
      headerName: "Tokens",
      width: 80,
      align: "center",
    },
    {
      field: "created_at",
      headerName: "Time",
      width: 160,
      valueGetter: (_value, row) => formatDate(row.created_at),
    },
  ];

  // ── Render chat bubble ──────────────────────────────────────────────────────

  function renderChatBubble(msg: MessageRow) {
    const style = getRoleStyle(msg.role);
    const isUser = msg.role === "user";
    const isSystem = msg.role === "system" || msg.role === "tool";

    if (isSystem) {
      return (
        <Box
          key={msg.id}
          sx={{
            display: "flex",
            justifyContent: "center",
            mb: 1.5,
          }}
        >
          <Box
            sx={{
              maxWidth: "80%",
              bgcolor: style.bg,
              borderRadius: 1.5,
              px: 2,
              py: 1,
              border: "1px solid",
              borderColor: "divider",
            }}
          >
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ fontWeight: 600, display: "block", mb: 0.5 }}
            >
              {style.label}
            </Typography>
            <Typography
              variant="body2"
              sx={{ fontFamily: "monospace", fontSize: "0.8rem", whiteSpace: "pre-wrap" }}
            >
              {msg.content}
            </Typography>
            <Typography
              variant="caption"
              color="text.disabled"
              sx={{ display: "block", mt: 0.5, textAlign: "right" }}
            >
              {formatTime(msg.created_at)}
              {msg.token_count > 0 && ` · ${msg.token_count} tokens`}
            </Typography>
          </Box>
        </Box>
      );
    }

    return (
      <Box
        key={msg.id}
        sx={{
          display: "flex",
          justifyContent: isUser ? "flex-end" : "flex-start",
          mb: 1.5,
        }}
      >
        <Box
          sx={{
            maxWidth: "70%",
            bgcolor: isUser ? style.bg : "#F5F5F5",
            color: isUser ? "#fff" : "text.primary",
            borderRadius: 2,
            borderBottomRightRadius: isUser ? 0 : 2,
            borderBottomLeftRadius: isUser ? 2 : 0,
            px: 2,
            py: 1.2,
          }}
        >
          <Typography
            variant="caption"
            sx={{
              fontWeight: 600,
              display: "block",
              mb: 0.5,
              opacity: 0.8,
              color: isUser ? "rgba(255,255,255,0.85)" : "text.secondary",
            }}
          >
            {isUser ? "You" : "Assistant"}
          </Typography>
          <Typography
            variant="body2"
            sx={{
              whiteSpace: "pre-wrap",
              lineHeight: 1.5,
              "& p": { mb: 1 },
            }}
          >
            {msg.content}
          </Typography>
          <Typography
            variant="caption"
            sx={{
              display: "block",
              mt: 0.5,
              textAlign: isUser ? "right" : "left",
              opacity: 0.6,
              color: isUser ? "rgba(255,255,255,0.7)" : "text.disabled",
            }}
          >
            {formatTime(msg.created_at)}
            {msg.token_count > 0 && ` · ${msg.token_count} tokens`}
          </Typography>
        </Box>
      </Box>
    );
  }

  // ── Render ──────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
        <CircularProgress />
      </Box>
    );
  }

  if (messages.length === 0) {
    return (
      <Card sx={{ p: 4, textAlign: "center" }}>
        <Typography variant="body1" color="text.secondary">
          No messages in this session yet.
        </Typography>
      </Card>
    );
  }

  return (
    <Box>
      {/* View toggle */}
      <Box sx={{ display: "flex", justifyContent: "flex-end", mb: 2 }}>
        <ToggleButtonGroup
          value={viewMode}
          exclusive
          onChange={(_e, val) => val && setViewMode(val)}
          size="small"
        >
          <ToggleButton value="chat">
            <ChatIcon fontSize="small" sx={{ mr: 0.5 }} />
            Chat
          </ToggleButton>
          <ToggleButton value="table">
            <TableChartIcon fontSize="small" sx={{ mr: 0.5 }} />
            Table
          </ToggleButton>
        </ToggleButtonGroup>
      </Box>

      {viewMode === "chat" ? (
        /* Chat bubble view */
        <Card sx={{ p: 3, maxHeight: "70vh", overflow: "auto" }}>
          {/* Load older button */}
          {hasMore && (
            <Box sx={{ display: "flex", justifyContent: "center", mb: 2 }}>
              <Button
                variant="outlined"
                size="small"
                onClick={loadOlder}
                disabled={loadingMore}
                startIcon={loadingMore ? <CircularProgress size={14} /> : undefined}
              >
                {loadingMore ? "Loading..." : "Load Older"}
              </Button>
            </Box>
          )}

          {messages.map(renderChatBubble)}
          <div ref={chatEndRef} />
        </Card>
      ) : (
        /* Table view */
        <Card>
          <DataGrid<MessageRow>
            rows={messages}
            columns={columns}
            loading={loading}
            getRowId={(row) => row.id}
            getRowHeight={() => 52}
            disableRowSelectionOnClick
            hideFooter
            autoHeight
            sx={{
              "& .MuiDataGrid-cell:focus": { outline: "none" },
            }}
          />
          {hasMore && (
            <Box sx={{ display: "flex", justifyContent: "center", p: 2 }}>
              <Button
                variant="outlined"
                onClick={loadOlder}
                disabled={loadingMore}
                startIcon={loadingMore ? <CircularProgress size={16} /> : undefined}
              >
                {loadingMore ? "Loading..." : "Load Older"}
              </Button>
            </Box>
          )}
        </Card>
      )}
    </Box>
  );
}
