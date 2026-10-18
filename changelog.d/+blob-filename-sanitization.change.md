Sanitize client-supplied blob file names to an inert basename (path separators, parent refs and control characters stripped, 255-char cap); blank names are rejected with 422.
