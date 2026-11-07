package main

import (
	"billiards/internal/gokit"
	"billiards/services/bookings"
)

func main() {
	gokit.Run("bookings", func(c *gokit.Context) gokit.Application { return bookings.New(c) })
}
