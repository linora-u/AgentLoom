package demo

type Reader interface {
	Read([]byte) (int, error)
}

type Pair[T any] struct {
	Left  T
	Right T
}

type ID = int64

func ComputeSum(values []int) int {
	sum := 0
	for _, v := range values {
		sum += v
	}
	return sum
}

func Convert[T any](v T) T {
	return v
}

type Worker struct {
	name string
}

func (w Worker) Name() string {
	return w.name
}

func (w *Worker) SetName(name string) {
	w.name = name
}
