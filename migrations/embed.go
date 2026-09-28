// Package migrations embeds the SQL migrations. They run at startup in file-name order.
package migrations

import "embed"

// FS holds every *.sql migration.
//
//go:embed *.sql
var FS embed.FS
