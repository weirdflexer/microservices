package main

import (
	"billiards/internal/gokit"
	"billiards/services/payments"
)

func main() {
	gokit.Run("payments", func(c *gokit.Context) gokit.Application { return payments.New(c) })
}
