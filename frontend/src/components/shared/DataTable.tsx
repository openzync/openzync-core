"use client";

import { Box, Button, Card, CircularProgress } from "@mui/material";
import { DataGrid, type GridColDef, type GridRowParams, type GridValidRowModel } from "@mui/x-data-grid";
import EmptyState from "./EmptyState";

interface DataTableProps<T extends GridValidRowModel> {
  rows: T[];
  columns: GridColDef<T>[];
  loading: boolean;
  getRowId: (row: T) => string;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyIcon?: React.ReactNode;
  emptyAction?: { label: string; onClick: () => void };
  onRowClick?: (params: GridRowParams<T>) => void;
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  getRowHeight?: () => number;
}

export default function DataTable<T extends GridValidRowModel>({
  rows,
  columns,
  loading,
  getRowId,
  emptyTitle = "No data found",
  emptyDescription = "There are no items to display yet.",
  emptyIcon,
  emptyAction,
  onRowClick,
  hasMore,
  loadingMore,
  onLoadMore,
  getRowHeight = () => 52,
}: DataTableProps<T>) {
  if (!loading && rows.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={emptyIcon}
          title={emptyTitle}
          description={emptyDescription}
          action={emptyAction}
        />
      </Card>
    );
  }

  return (
    <Card>
      <DataGrid<T>
        rows={rows}
        columns={columns}
        loading={loading}
        getRowId={getRowId}
        getRowHeight={getRowHeight}
        disableRowSelectionOnClick
        hideFooter
        autoHeight
        onRowClick={onRowClick}
        sx={{ "& .MuiDataGrid-cell:focus": { outline: "none" } }}
        slotProps={{
          loadingOverlay: {
            variant: "skeleton",
            noRowsVariant: "skeleton",
          },
        }}
      />
      {hasMore && onLoadMore && (
        <Box sx={{ display: "flex", justifyContent: "center", p: 2 }}>
          <Button
            variant="outlined"
            onClick={onLoadMore}
            disabled={loadingMore}
            startIcon={loadingMore ? <CircularProgress size={16} /> : undefined}
          >
            {loadingMore ? "Loading..." : "Load More"}
          </Button>
        </Box>
      )}
    </Card>
  );
}
