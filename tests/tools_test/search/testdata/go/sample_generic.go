package demo

type Box[T any] struct {
    Value T
}

type Handler[T any] interface {
    Handle(T)
}

type Count = int
